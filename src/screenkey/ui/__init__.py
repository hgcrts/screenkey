"""PyQt5 界面层。

导入顺序很重要：``qt_env`` **必须**在任何其他 Qt 子模块之前导入，
否则 Qt 会在寻找平台插件时直接以 0xC0000409 崩溃（见 :mod:`.qt_env` 的说明）。
"""

from . import qt_env  # noqa: F401  —— 必须最先执行，用来修 Qt 插件路径

qt_env.configure_qt_environment()

from .danmaku_window import DanmakuWindow
from .filter_editor import FilterEditorDialog
from .login_dialog import LoginDialog
from .main_window import MainWindow
from .mapping_editor import MappingEditorDialog
from .theme import COLORS, STYLESHEET, danmaku_html, escape_html
from .worker import EngineBridge

__all__ = [
    "COLORS",
    "STYLESHEET",
    "DanmakuWindow",
    "EngineBridge",
    "FilterEditorDialog",
    "LoginDialog",
    "MainWindow",
    "MappingEditorDialog",
    "danmaku_html",
    "escape_html",
    "qt_env",
]
