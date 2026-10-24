# -*- coding: utf-8 -*-
"""``python -m danmaku_kb`` 的入口。

只做一件事：转交给 :func:`danmaku_kb.main.main`。真正的引导（解析参数、配 Qt
环境、拉起界面）全在 ``main.py`` 里——它同时也是 ``python src\\danmaku_kb\\main.py``
和 PyInstaller 打包产物的唯一入口，所以这里**不复制**任何逻辑。

注意 ``python -m danmaku_kb`` 要求 ``src`` 已在 ``sys.path``（在 ``src`` 目录下
运行，或设 ``PYTHONPATH=src``）。用户双击的 ``启动.bat`` 自己会处理路径。
"""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
