"""Qt 环境修复 —— 必须在 ``import PyQt5`` 之前导入。

参考项目踩过的坑
----------------
Windows + Python 3.12 的 venv 里，``.venv\\Scripts\\python.exe`` 是一个**重定向器**，
Qt 内部用 ``applicationDirPath()`` 推断插件目录时会指到 ``Scripts\\``，
找不到 ``platforms\\qwindows.dll``，于是：

    qt.qpa.plugin: Could not find the Qt platform plugin "windows"
    This application failed to start because no Qt platform plugin could be initialized.

进程以 ``-1073740791`` (0xC0000409) 退出，**连 Python 的异常都来不及抛**，
日志里什么都看不到，非常难查。

修法（在导入 PyQt5 之前执行）：
1. 把 ``QT_QPA_PLATFORM_PLUGIN_PATH`` 指向 ``PyQt5\\Qt5\\plugins\\platforms``；
2. 把 ``QT_PLUGIN_PATH`` 指向 ``PyQt5\\Qt5\\plugins``；
3. 把 ``PyQt5\\Qt5\\bin`` 插到 ``PATH`` 最前（qwindows.dll 依赖那里的 Qt5Core.dll 等）。

本模块只做这件事，导入它没有任何副作用（幂等，不启动 QApplication）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

__all__ = ["configure_qt_environment", "qt_plugin_path", "PYQT5_AVAILABLE", "import_error"]

_configured = False
qt_plugin_path: Optional[Path] = None
import_error: str = ""

#: 导入成功的 PyQt5 包对象（未安装时为 None）。保留引用而不是只 import 一下，
#: 既让探测结果可复用，也避免"导入却从不使用"的告警。
_pyqt5_pkg = None

try:  # pragma: no cover - 取决于环境
    import PyQt5 as _pyqt5_pkg

    PYQT5_AVAILABLE = True
except Exception as exc:  # pragma: no cover
    PYQT5_AVAILABLE = False
    import_error = str(exc)


def _find_plugin_root() -> Optional[Path]:
    """定位 ``PyQt5\\Qt5\\plugins``。"""

    candidates = []
    if _pyqt5_pkg is not None:
        try:
            pkg = Path(_pyqt5_pkg.__file__).resolve().parent
            candidates += [pkg / "Qt5" / "plugins", pkg / "Qt" / "plugins", pkg / "plugins"]
        except Exception:  # pragma: no cover - 极端安装形态
            pass

    # 有些安装把 Qt 放在 PyQt5-Qt5 分发里，路径与 PyQt5 同级。
    candidates.append(Path(sys.prefix) / "Lib" / "site-packages" / "PyQt5" / "Qt5" / "plugins")

    for path in candidates:
        if path.is_dir() and (path / "platforms").is_dir():
            return path
    for path in candidates:
        if path.is_dir():
            return path
    return None


def configure_qt_environment(verbose: bool = False) -> Optional[Path]:
    """设置 Qt 插件环境变量。成功后不再重复执行；失败则允许下次重试。"""

    global _configured, qt_plugin_path
    if _configured:
        return qt_plugin_path

    if not PYQT5_AVAILABLE:
        # 不置 _configured：PyQt5 缺失也可能是"暂时还没装好"，
        # 以前在这里就把 _configured 置 True，导致后续再调用直接返回 None，
        # 永远没有重试机会。
        return None

    root = _find_plugin_root()
    if root is None:
        # 同理：插件目录没找到（例如打包后路径还没就绪）时不锁定状态。
        if verbose:
            print("[qt_env] 未能定位 Qt 插件目录，依赖 Qt 自身的搜索逻辑", file=sys.stderr)
        return None

    _configured = True
    qt_plugin_path = root
    platforms = root / "platforms"
    if platforms.is_dir():
        # setdefault 而不是直接赋值：打包运行时（PyInstaller 的 _MEIPASS）
        # 可能已经由启动器或 Qt 自身设好了正确路径，无条件覆盖会把它盖掉。
        os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(platforms))
    os.environ.setdefault("QT_PLUGIN_PATH", str(root))

    bin_dir = root.parent / "bin"
    if bin_dir.is_dir():
        existing = os.environ.get("PATH", "")
        if str(bin_dir) not in existing.split(os.pathsep):
            os.environ["PATH"] = str(bin_dir) + os.pathsep + existing

    # 避免高分屏下界面糊成一片
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

    if verbose:
        print(f"[qt_env] Qt 插件目录 = {root}", file=sys.stderr)
        print(f"[qt_env] QT_QPA_PLATFORM_PLUGIN_PATH = {os.environ.get('QT_QPA_PLATFORM_PLUGIN_PATH')}", file=sys.stderr)
    return root
