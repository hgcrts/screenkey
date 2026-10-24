# -*- coding: utf-8 -*-
"""前台窗口探测 + 「焦点窗口」匹配。

用途
----
主播开着游戏/剪辑软件的时候，幕键应该只往**那个**窗口里发按键；一旦切到
浏览器、聊天软件，按键就不该乱打进去（否则观众能隔着弹幕操作主播的其他程序）。

这个模块只做两件事：

1. 读当前前台窗口是谁（标题 / 进程名 / 窗口类名）——:func:`get_foreground_window`
2. 判断它是否符合用户填的目标——:func:`window_matches` / :class:`FocusFilter`

设计要点
--------
* **匹配逻辑与 Win32 解耦**：:func:`window_matches` 收一个 :class:`WindowInfo`，
  不碰任何系统调用，所以能在任何平台上被完整测试。
* **非 Windows 平台不拦**：拿不到前台窗口信息时 :meth:`FocusFilter.should_inject`
  返回 ``True``。这一层是"往哪儿发"的过滤器，不是"能不能发"的能力判断；
  真发不出去由 :mod:`screenkey.input.keycodes` 如实返回 ``False``，不要把
  失败原因盖成"焦点不匹配"。
* **不做缓存**：每次判定都重新读一次。前台窗口可能在两次弹幕之间换掉，
  而缓存 window handle 是有真风险的（Windows 会复用已销毁窗口的 HWND，
  缓存命中就会把新窗口当成旧窗口）。实测这几个调用加起来是微秒量级，
  相对于弹幕频率完全不值得用正确性去换。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, List, Optional

from ..utils.logging_setup import get_logger

logger = get_logger("input.window")

# ----------------------------------------------------------------------
# Windows API 探测（与 keycodes.py 同样的写法：非 Windows 上模块仍可导入）
# ----------------------------------------------------------------------

WINDOWS_API = False
ctypes = None  # type: ignore[assignment]
wintypes = None  # type: ignore[assignment]

if sys.platform == "win32":  # pragma: no cover - 平台相关
    try:
        import ctypes as _ctypes
        from ctypes import wintypes as _wintypes

        ctypes = _ctypes
        wintypes = _wintypes
        WINDOWS_API = True
    except Exception:  # pragma: no cover
        WINDOWS_API = False

#: 参与匹配的窗口字段
FOCUS_FIELDS = ("title", "process", "class")
#: 匹配方式
FOCUS_MATCH_MODES = ("contains", "exact", "regex")

#: 字段名 → 界面中文名
FIELD_LABELS = {
    "title": "窗口标题",
    "process": "进程名（推荐，标题会变）",
    "class": "窗口类名",
}
#: 匹配方式 → 界面中文名
MODE_LABELS = {
    "contains": "包含",
    "exact": "完全相等",
    "regex": "正则表达式",
}


def coerce_field(value: Any) -> str:
    """把任意输入收敛到 :data:`FOCUS_FIELDS` 里的一个值。"""
    text = str(value or "").strip().lower()
    return text if text in FOCUS_FIELDS else "title"


def coerce_mode(value: Any) -> str:
    """把任意输入收敛到 :data:`FOCUS_MATCH_MODES` 里的一个值。"""
    text = str(value or "").strip().lower()
    return text if text in FOCUS_MATCH_MODES else "contains"


# ----------------------------------------------------------------------
# 窗口信息
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class WindowInfo:
    """一个窗口的标识信息。

    :param hwnd: 窗口句柄
    :param title: 窗口标题
    :param class_name: 窗口类名（``GetClassNameW``）
    :param process_name: 进程可执行文件名，含后缀，如 ``notepad.exe``
    :param pid: 进程 ID
    """

    hwnd: int = 0
    title: str = ""
    class_name: str = ""
    process_name: str = ""
    pid: int = 0

    @property
    def label(self) -> str:
        """给下拉框/状态栏显示的一行文字。"""
        title = self.title or "(无标题)"
        if self.process_name:
            return f"{title}　[{self.process_name}]"
        return title

    def field_value(self, field: str) -> str:
        """按字段名取值；未知字段回落到标题。"""
        if field == "class":
            return self.class_name
        if field == "process":
            return self.process_name
        return self.title


# ----------------------------------------------------------------------
# 匹配（纯逻辑，可离线测试）
# ----------------------------------------------------------------------


def _strip_exe(name: str) -> str:
    """去掉 ``.exe`` 后缀，便于"填 notepad 也能匹配 notepad.exe"。"""
    lowered = name.strip().lower()
    return lowered[:-4] if lowered.endswith(".exe") else lowered


def window_matches(
    info: Optional[WindowInfo],
    pattern: str,
    *,
    field: str = "title",
    mode: str = "contains",
    case_sensitive: bool = False,
) -> bool:
    """判断窗口是否匹配目标。

    :param info: 前台窗口信息；``None``（取不到）一律不匹配
    :param pattern: 用户填的目标
    :param field: ``"title"`` / ``"process"`` / ``"class"``
    :param mode: ``"contains"`` / ``"exact"`` / ``"regex"``
    :param case_sensitive: 是否区分大小写

    ``exact`` 模式下比对进程名时会自动忽略 ``.exe`` 后缀——
    用户填 ``genshinimpact`` 与填 ``GenshinImpact.exe`` 是同一个意思。

    正则**写错时按"不匹配"处理**（fail closed）。这条是刻意选的：正则写错
    却继续注入，等于把按键打进用户当时正在看的任意窗口里，正是这个功能要
    防的事。界面上有即时状态可以看出来，日志里也会留一条 warning。
    """
    if info is None:
        return False
    pattern = str(pattern or "").strip()
    if not pattern:
        # 空目标不能匹配一切：那等于"启用了过滤但什么都不填"，用户的预期
        # 是"还没配好"，不是"所有窗口都放行"。
        return False

    field = coerce_field(field)
    mode = coerce_mode(mode)
    raw_value = info.field_value(field)
    if not raw_value:
        return False

    if mode == "exact":
        if case_sensitive:
            if raw_value == pattern:
                return True
            return field == "process" and _strip_exe(raw_value) == _strip_exe(pattern)
        if raw_value.lower() == pattern.lower():
            return True
        return field == "process" and _strip_exe(raw_value) == _strip_exe(pattern)

    if mode == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            return re.search(pattern, raw_value, flags) is not None
        except re.error as exc:
            logger.warning("焦点窗口正则无效，按不匹配处理：%r（%s）", pattern, exc)
            return False

    # contains（默认）
    if case_sensitive:
        return pattern in raw_value
    return pattern.lower() in raw_value.lower()


# ----------------------------------------------------------------------
# Win32 探测
# ----------------------------------------------------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_GW_OWNER = 4


def _ensure_prototypes() -> None:  # pragma: no cover - 平台相关
    """给 ctypes 函数声明参数类型。

    **必须显式声明**：不声明的话 ``GetWindowTextW`` 之类的宽字符 API 会按
    ``c_int`` 处理返回值/参数，64 位下句柄被截断，结果是"偶尔读不到标题"
    这种难查的随机行为。
    """
    global _prototypes_ready
    if _prototypes_ready or not WINDOWS_API:
        return
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND

        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
    except Exception as exc:  # pragma: no cover
        logger.debug("声明 Win32 原型失败（不影响主流程）：%s", exc)
    _prototypes_ready = True


_prototypes_ready = False


def _window_text(hwnd: int) -> str:  # pragma: no cover - 平台相关
    user32 = ctypes.windll.user32
    length = int(user32.GetWindowTextLengthW(hwnd))
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _class_name(hwnd: int) -> str:  # pragma: no cover - 平台相关
    user32 = ctypes.windll.user32
    buffer = ctypes.create_unicode_buffer(256)
    if user32.GetClassNameW(hwnd, buffer, 256) <= 0:
        return ""
    return buffer.value


def _process_name(pid: int) -> str:  # pragma: no cover - 平台相关
    """由 PID 取进程可执行文件名（只留文件名，不要完整路径）。"""
    if not pid:
        return ""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 提权进程（比如某些带反作弊的游戏）拒绝被查询是正常的，不是错误。
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        path = buffer.value
    finally:
        kernel32.CloseHandle(handle)
    return path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]


def _describe(hwnd: int) -> WindowInfo:  # pragma: no cover - 平台相关
    """把 handle 变成一个 :class:`WindowInfo`。"""
    user32 = ctypes.windll.user32
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return WindowInfo(
        hwnd=int(hwnd or 0),
        title=_window_text(hwnd),
        class_name=_class_name(hwnd),
        process_name=_process_name(int(pid.value)),
        pid=int(pid.value),
    )


def get_foreground_window() -> Optional[WindowInfo]:
    """当前前台窗口；取不到返回 ``None``（非 Windows / 调用失败）。"""
    if not WINDOWS_API:
        return None
    _ensure_prototypes()
    try:  # pragma: no cover - 平台相关
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
        return _describe(hwnd)
    except Exception as exc:  # pragma: no cover
        logger.debug("读取前台窗口失败：%s", exc)
        return None


def list_windows(*, include_untitled: bool = False, limit: int = 300) -> List[WindowInfo]:
    """枚举当前可见的顶层窗口，给界面「选择窗口…」用。

    过滤规则：可见 + 不是别人的附属弹窗（``GW_OWNER`` 为 0）+ 默认要有标题。
    同名同进程的窗口只留一个——主播的游戏通常只有一个主窗口，列表太长反而难选。
    """
    if not WINDOWS_API:
        return []
    _ensure_prototypes()
    found: List[WindowInfo] = []
    seen = set()
    collected: List[int] = []

    try:  # pragma: no cover - 平台相关
        user32 = ctypes.windll.user32
        enum_proc_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def _callback(hwnd, _lparam):  # pragma: no cover - 平台相关
            try:
                if len(collected) >= limit:
                    return False
                if not user32.IsWindowVisible(hwnd):
                    return True
                if user32.GetWindow(hwnd, _GW_OWNER):
                    return True
                if not include_untitled and user32.GetWindowTextLengthW(hwnd) <= 0:
                    return True
                collected.append(int(hwnd))
            except Exception:
                return True
            return True

        callback = enum_proc_type(_callback)
        user32.EnumWindows(callback, 0)
    except Exception as exc:  # pragma: no cover
        logger.debug("枚举窗口失败：%s", exc)
        return []

    for hwnd in collected:  # pragma: no cover - 平台相关
        try:
            info = _describe(hwnd)
        except Exception:
            continue
        key = (info.process_name, info.title)
        if key in seen:
            continue
        seen.add(key)
        found.append(info)
    found.sort(key=lambda w: (w.title or "\uffff").lower())
    return found


# ----------------------------------------------------------------------
# 过滤器
# ----------------------------------------------------------------------


class FocusFilter:
    """按配置判断"现在这个前台窗口能不能收按键"。

    刻意**不**持有 :class:`~screenkey.config.models.FocusFilterConfig`，
    而是收几项标量（:meth:`from_config` 负责从任意配置对象上取）。原因是分层
    约定：``input`` 层不依赖 ``config`` 层，装配由 :mod:`screenkey.engine`
    这个唯一装配点完成。引擎每次判定都用当前配置新建一个，所以配置改完
    立刻生效、不存在"忘了同步"的中间态。
    """

    def __init__(
        self,
        enabled: bool = False,
        pattern: str = "",
        field: str = "title",
        mode: str = "contains",
        case_sensitive: bool = False,
    ) -> None:
        self.enabled = bool(enabled)
        self.pattern = str(pattern or "")
        self.field = coerce_field(field)
        self.mode = coerce_mode(mode)
        self.case_sensitive = bool(case_sensitive)

    @classmethod
    def from_config(cls, cfg: Any) -> "FocusFilter":
        """从任意带同名属性的配置对象构造（duck typing，不 import config）。"""
        if cfg is None:
            return cls()
        return cls(
            enabled=bool(getattr(cfg, "enabled", False)),
            pattern=str(getattr(cfg, "pattern", "") or ""),
            field=getattr(cfg, "field", "title"),
            mode=getattr(cfg, "match_mode", "contains"),
            case_sensitive=bool(getattr(cfg, "case_sensitive", False)),
        )

    @property
    def configured(self) -> bool:
        """是否真的配好了（启用 + 目标非空）。"""
        return bool(self.enabled and self.pattern.strip())

    def matches(self, info: Optional[WindowInfo]) -> bool:
        """给定窗口是否匹配。"""
        return window_matches(
            info,
            self.pattern,
            field=self.field,
            mode=self.mode,
            case_sensitive=self.case_sensitive,
        )

    def should_inject(self) -> bool:
        """当前是否允许注入按键。

        - 没启用 / 没填目标 → ``True``（不过滤）
        - 非 Windows → ``True``（让下层去如实报"发不出去"）
        - 其它 → 看前台窗口是否匹配；**取不到前台窗口也算不匹配**
          （宁可不发，也不要把按键打进不知道哪儿的窗口）

        探测本身抛异常时同样按"不匹配"处理：这个方法是"要不要把按键发出去"
        的最后一道闸，任何意外都只能往"不发"的方向倒。真正的疑点会以
        warning 记进日志，不会静默消失。
        """
        if not self.configured:
            return True
        if not WINDOWS_API:
            return True
        try:
            return self.matches(get_foreground_window())
        except Exception as exc:  # pragma: no cover - 平台相关
            logger.warning("读取前台窗口失败，按不匹配处理（不注入按键）：%s", exc)
            return False

    def status_text(self) -> str:
        """给界面状态标签用的一句话。

        这个方法的返回值会被塞进 Qt 控件的 ``setText``，而它是在 GUI 线程的
        定时器里被调用的：**任何异常在这里逃出去都会变成 PyQt5 的
        ``qFatal`` 闪退**（无 traceback、退出码 0xC0000409）。所以探测一律
        包起来，失败就报一句人话。
        """
        if not self.enabled:
            return "未启用：按键会注入到当前任意窗口。"
        if not self.pattern.strip():
            return "已启用但还没填目标窗口：现在不会注入任何按键。"
        if not WINDOWS_API:
            return "当前平台读不到前台窗口，过滤不生效。"
        try:
            info = get_foreground_window()
        except Exception as exc:  # pragma: no cover - 平台相关
            logger.warning("读取前台窗口失败：%s", exc)
            return "读取前台窗口出错：为保证安全，现在不会注入按键（详见日志）。"
        if info is None:
            return "读不到前台窗口：为保证安全，现在不会注入按键。"
        allowed = self.matches(info)
        verdict = "会注入按键" if allowed else "不注入（窗口不匹配）"
        return f"当前前台：{info.label} → {verdict}"


__all__ = [
    "FIELD_LABELS",
    "FOCUS_FIELDS",
    "FOCUS_MATCH_MODES",
    "FocusFilter",
    "MODE_LABELS",
    "WINDOWS_API",
    "WindowInfo",
    "coerce_field",
    "coerce_mode",
    "get_foreground_window",
    "list_windows",
    "window_matches",
]
