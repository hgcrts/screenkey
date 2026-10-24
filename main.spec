# -*- mode: python ; coding: utf-8 -*-
"""弹幕键盘 V2 —— PyInstaller 打包配置。

用法（在项目根目录）::

    .venv\\Scripts\\python.exe -m PyInstaller --noconfirm --clean main.spec

产物是 onedir 形态：``dist/弹幕键盘V2/弹幕键盘V2.exe`` 加一个 ``_internal`` 目录，
必须整个目录一起拷贝，不能只拿走 exe。

关于 data/ 目录
--------------
``src/danmaku_kb/data/sign.js`` 是抖音弹幕连接离不开的资源：

* ``sign.js`` —— **第三方、已混淆的抖音 SDK 代码**（``webmssdk.js`` 系列，480KB+）。
  它在 WebSocket URL 上计算 ``signature`` 参数，纯 Python 无法复现。
  这里随包携带**仅用于研究学习**，版权属于抖音，请勿商用或二次分发。
  想换成自己的副本，可设环境变量 ``DANMAKU_KB_SIGN_JS`` 指向你的文件。

``danmaku/signature.py`` 用 ``Path(__file__).resolve().parent.parent / "data"``
定位它；打包后模块的 ``__file__`` 落在 ``<_MEIPASS>/danmaku_kb/danmaku/...``，
所以这里的 datas 目标目录必须写成 ``danmaku_kb/data``，两边才对得上。
"""

import sys
from pathlib import Path

# SPECPATH 由 PyInstaller 注入，指向本 spec 所在目录（项目根目录）
ROOT = Path(SPECPATH).resolve()
PKG = ROOT / "src" / "danmaku_kb"
DATA = PKG / "data"

# ----------------------------------------------------------------------
# 随包携带的数据文件
# ----------------------------------------------------------------------
datas = [
    # (源文件, 打包后的相对目录)
    (str(DATA / "sign.js"), "danmaku_kb/data"),
]

# ----------------------------------------------------------------------
# 动态导入、插件式后端：静态分析扫不到的，显式列出来
# ----------------------------------------------------------------------
hiddenimports = [
    # B 站后端（blivedm 0.1.x 是**扁平单文件包**：只有 client / handlers /
    # models 三个模块，没有 clients/ 与 models/ 子包。写成
    # "blivedm.clients"、"blivedm.models.web" 会让 PyInstaller 找不到模块而告警，
    # 更糟的是打包后的程序里 BLIVEDM_AVAILABLE 会变成 False，
    # 于是 B 站抓取器谎报"缺少 blivedm 依赖"——源码侧同样的坑已修过一次。）
    "blivedm.client",
    "blivedm.handlers",
    "blivedm.models",
    # 抖音后端
    "websockets",
    "aiohttp",
    # 界面与输入
    "pynput",
    "pynput.keyboard._win32",
    "pynput.mouse._win32",
    "segno",
    "requests",
]

# 用不到的重型依赖，排除掉能显著缩小体积
excludes = [
    "tkinter",
    "matplotlib",
    "numpy",
    "pytest",
    "playwright",   # 抖音 browser 模式的可选后端，默认走 websocket
    "pyautogui",    # 备用键鼠后端，默认不用
]

a = Analysis(
    [str(PKG / "main.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="弹幕键盘V2",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # Qt 的 DLL 被 UPX 压过之后有概率加载失败，直接关掉
    upx=False,
    console=False,          # GUI 程序，不弹控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="弹幕键盘V2",
)
