# -*- coding: utf-8 -*-
"""前台窗口判定：决定"这一下按键到底该不该发出去"。

这个模块是引擎里唯一一处关心"现在前台是哪个窗口"的地方。它被单独拿出来
有两个原因：

1. **它必须每次重算，不能缓存。** 配置对象（``AppConfig``）是界面线程随时
   会改的，持有的快照要在每个改动点同步，漏一处就变成"界面上关了过滤、
   实际还在拦"。所以 ``FocusGate`` 只持有配置**引用**，每次判定都按当下的
   配置重新构造 ``FocusFilter`` —— 几次属性拷贝，代价可以忽略。
2. **它不碰锁、不碰统计、不碰按键。** 引擎里这段逻辑原来夹在注入流程中间，
   想单测一个"前台窗口不匹配"就得先搭好键盘和抓取器。现在它可以脱离引擎
   直接测（构造一个配置，换掉前台窗口，问 ``blocks()``）。

"当前放行吗"有三个不同的问题，别混用：

===================================  ===================================
``allows_injection()``               给界面显示状态用："现在按下去会生效吗"
``status_text()``                    给界面显示原因用："为什么没生效"
``blocks()``                         给注入流程用："这一下要不要跳过"
===================================  ===================================

``blocks()`` 与 ``allows_injection()`` 的区别不止是取反：``blocks()`` 还会
回答"压根没配置焦点过滤"这种情况（返回 ``False``，也就是不拦），而
``allows_injection()`` 对未配置的情形直接返回 ``True``。
"""

from __future__ import annotations

from .config.models import AppConfig
from .input.window import FocusFilter

__all__ = ["FocusGate"]


class FocusGate:
    """按当前配置判定前台窗口是否允许注入。

    :param config: 应用配置（只读引用，不复制）
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    def filter(self) -> FocusFilter:
        """按**当前**配置构造焦点过滤器。

        ``focus_filter`` 字段缺失（旧配置对象/旧 settings.json）时按"没启用"
        处理，这也是 ``FocusFilter.from_config(None)`` 的语义。
        """
        return FocusFilter.from_config(getattr(self._config.input, "focus_filter", None))

    def allows_injection(self, dry_run: bool = False) -> bool:
        """当前是否允许注入按键。

        空跑（``dry_run``，自检与单测用）一律放行：那时本来就不注入，再让
        结果依赖"跑的时候恰好哪个窗口在前台"只会让自检不可复现。
        """
        return dry_run or self.filter().should_inject()

    def status_text(self, dry_run: bool = False) -> str:
        """给界面看的焦点过滤状态文案。"""
        if dry_run:
            return "空跑（自检）模式：不做焦点判定。"
        return self.filter().status_text()

    def blocks(self, dry_run: bool = False) -> bool:
        """这一下是否应该因为前台窗口不对而跳过。

        未配置焦点过滤时返回 ``False``（不拦）。**故意不做**的是"把按键发
        出去再让系统忽略"：这个功能的意义就是按键根本不该出现在别的窗口里，
        所以拦在注入之前。
        """
        if dry_run:
            return False
        focus = self.filter()
        if not focus.configured:
            return False
        return not focus.should_inject()
