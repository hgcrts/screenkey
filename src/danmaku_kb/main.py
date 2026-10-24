"""弹幕键盘 V2 —— 程序入口。

用法::

    python main.py                 # 正常启动界面
    python main.py --selftest      # 不开界面，跑一遍离线自检
    python main.py --check-env     # 检查本机注入环境（按键发不出去时先跑这个）
    python main.py --simulate      # 启动后直接进入模拟模式
    python main.py --debug         # 控制台输出 DEBUG 日志

退出码：0 正常；1 参数错误；2 环境缺依赖；3 自检失败。

（`argparse` 默认用 2 表示参数错误，那会和这里的 `EXIT_ENV = 2`「缺少依赖」撞车，
所以下面自己接管了 `error()`，把参数错误的退出码改成 `EXIT_ARGS`。）
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import NoReturn

# 允许直接 `python main.py`（把 src 加入 sys.path），也允许 `python -m danmaku_kb.main`
#
# 这里是 `parent.parent` 而不是 `parent`，因为本文件位于 `src/danmaku_kb/main.py`，
# 而下面 `from danmaku_kb import ...` 需要的是**包目录的父目录**（也就是 `src`）。
# 写成 `.parent` 时插进去的是包目录自身，`python src\danmaku_kb\main.py` 必然
# `ModuleNotFoundError: No module named 'danmaku_kb'` —— 只有外部先设好
# `PYTHONPATH=src` 才能启动，而所有测试恰好都替它设了 PYTHONPATH，
# 于是这个缺陷一直没被发现：用户双击 `启动.bat` 直接失败（见 README「常见问题」）。
_PACKAGE_DIR = Path(__file__).resolve().parent  # ...\src\danmaku_kb
_SRC = _PACKAGE_DIR.parent  # ...\src —— 只有它能让 `import danmaku_kb` 成功
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from danmaku_kb import __version__  # noqa: E402
from danmaku_kb.utils.logging_setup import get_logger, setup_logging  # noqa: E402

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_ARGS = 1
EXIT_ENV = 2
EXIT_SELFTEST = 3


def _forgiving_std_streams() -> None:
    """把 stdout/stderr 的编码错误从"抛异常"改成"替换成 ?"。

    中文 Windows 的控制台默认是 GBK，编不了的字符会让 ``print`` 抛
    ``UnicodeEncodeError``。实测被一个带 🔍 的浏览器标签页标题打中过：
    ``--selftest`` 的那一行"焦点窗口 —— 当前前台 <窗口标题>"直接抛异常，
    于是自检谎报"3 项失败"（其实 10 项都过了）。窗口标题、房间名都是用户
    输入，不能假设控制台编得了。

    ``reconfigure`` 需要 Python 3.7+；拿不到（被重定向成别的对象）就跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError, OSError):  # pragma: no cover
            pass


class _ArgumentParser(argparse.ArgumentParser):
    """把「参数写错」的退出码从 argparse 默认的 2 改成 `EXIT_ARGS`(1)。

    不这么做的话，退出码 2 会同时表示「命令行参数错误」和「环境缺少 PyQt5」，
    调用方（比如 `启动.bat` 或用户自己的脚本）没法只看退出码判断该重敲命令还是该装依赖。
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_ARGS, f"{self.prog}: error: {message}\n")


def parse_args(argv=None) -> argparse.Namespace:
    parser = _ArgumentParser(
        prog="danmaku-kb",
        description="弹幕键盘 V2 —— 把直播间弹幕映射成本地键鼠操作",
    )
    parser.add_argument("--version", action="version", version=f"弹幕键盘 V2 {__version__}")
    parser.add_argument("--debug", action="store_true", help="输出 DEBUG 级日志")
    parser.add_argument("--log-dir", default="", help="日志目录（默认 ~/.danmaku_kb/logs）")
    parser.add_argument("--config-dir", default="", help="配置目录（默认 ~/.danmaku_kb）")
    parser.add_argument("--selftest", action="store_true", help="只跑离线自检，不打开界面")
    parser.add_argument(
        "--check-env",
        action="store_true",
        help="检查本机注入环境（权限/沙箱/前台窗口/配置目录），排查“按键发不出去”",
    )
    parser.add_argument("--simulate", action="store_true", help="启动后自动进入模拟弹幕模式")
    parser.add_argument("--platform", choices=("douyin", "bilibili"), default="", help="启动后自动连接的平台")
    parser.add_argument("--room", default="", help="启动后自动连接的房间号")
    parser.add_argument("--no-hotkey", action="store_true", help="不注册全局热键")
    return parser.parse_args(argv)


def run_selftest() -> int:
    """离线自检：不联网、不开界面，验证核心链路是否可用。"""

    print(f"弹幕键盘 V2 {__version__} 自检")
    failures = []

    def check(name: str, fn) -> None:
        try:
            detail = fn()
            print(f"  [OK]   {name}" + (f" —— {detail}" if detail else ""))
        except Exception as exc:
            failures.append((name, exc))
            print(f"  [FAIL] {name} —— {type(exc).__name__}: {exc}")

    def _imports():
        # 用 importlib 而不是一串 ``import ... # noqa``：这是"验证这些模块
        # 能导入"的检查，不是真的要用它们，写成 import 语句只会让静态检查
        # 一直报 unused import，也让后来人分不清哪些 import 是负载、哪些是探针。
        import importlib

        modules = (
            "danmaku_kb.config.manager",
            "danmaku_kb.danmaku.douyin",
            "danmaku_kb.danmaku.bilibili",
            "danmaku_kb.danmaku.protobuf_lite",
            "danmaku_kb.filters.rules",
            "danmaku_kb.input.keycodes",
            "danmaku_kb.input.matcher",
            "danmaku_kb.login.bilibili_qr",
        )
        for name in modules:
            importlib.import_module(name)

        return "核心模块全部可导入"

    def _mappings():
        from danmaku_kb.input.mappings import default_key_mappings, default_mouse_mappings

        k, m = default_key_mappings(), default_mouse_mappings()
        if not k or not m:
            raise AssertionError("默认映射为空")
        return f"键盘 {len(k)} 条 / 鼠标 {len(m)} 条"

    def _match():
        from danmaku_kb.input.mappings import default_key_mappings
        from danmaku_kb.input.matcher import match_content

        found = match_content("666666", default_key_mappings(), max_results=3)
        if not found:
            raise AssertionError("666666 应该命中感应长按")
        top = found[0]
        if not top.is_long_press:
            raise AssertionError("666666 应判定为长按")
        return f"{top.mapping.trigger} → {top.kind}（长按）"

    def _hotkey():
        from danmaku_kb.input.hotkey import normalize_hotkey

        got = normalize_hotkey("shift+Ctrl+Q")
        if got != "ctrl+q+shift":
            raise AssertionError(f"归一化结果异常：{got}")
        return got

    def _filter():
        from danmaku_kb.danmaku.message import DanmakuMessage, MessageType
        from danmaku_kb.filters.rules import FilterEngine

        engine = FilterEngine.default()
        enter = DanmakuMessage(content="进入了直播间", msg_type=MessageType.ENTER)
        chat = DanmakuMessage(content="666", msg_type=MessageType.DANMAKU)
        if engine.evaluate(enter).actionable:
            raise AssertionError("进入直播间事件不应触发按键")
        if not engine.evaluate(chat).actionable:
            raise AssertionError("普通弹幕应可触发按键")
        return "默认规则正确"

    def _protobuf():
        from danmaku_kb.danmaku.protobuf_lite import (
            encode_field_string,
            get_str,
            parse_message,
        )

        fields = parse_message(encode_field_string(2, "hello"))
        if get_str(fields, 2) != "hello":
            raise AssertionError("protobuf 往返失败")
        return "wire-format 往返正常"

    def _engine():
        from danmaku_kb.engine import DanmakuKeyboardEngine
        from danmaku_kb.danmaku.simulator import SimulatorFetcher

        engine = DanmakuKeyboardEngine()
        engine.dry_run = True
        fetcher = SimulatorFetcher(interval=0.01)
        engine.bind_fetcher(fetcher)
        events = []
        engine.on_event = events.append
        fetcher.emit_sequence(["666666", "w", "左键"])
        actions = [e for e in events if e.kind == "action"]
        if len(actions) != 3:
            raise AssertionError(f"应有 3 个动作事件，实际 {len(actions)}")
        return f"3 条弹幕 → {len(actions)} 个动作"

    def _config(tmp_dir: str):
        from danmaku_kb.config.manager import ConfigManager

        manager = ConfigManager(config_dir=tmp_dir, auto_save=False)
        manager.load()
        manager.save()
        if not manager.exists:
            raise AssertionError("配置文件未写出")
        return "配置读写正常"

    def _qr():
        from danmaku_kb.login.qr_image import make_qr_png

        data = make_qr_png("https://example.com/test")
        if not data or not data.startswith(b"\x89PNG"):
            raise AssertionError("二维码渲染失败")
        return f"{len(data)} 字节 PNG"

    def _gui():
        from danmaku_kb.ui.qt_env import PYQT5_AVAILABLE, configure_qt_environment

        if not PYQT5_AVAILABLE:
            raise RuntimeError("未安装 PyQt5")
        root = configure_qt_environment()
        # 这里只验证"Qt 绑定与插件目录都到位"，**不能**真的实例化 QApplication：
        # 自检必须能在没有显示设备的环境里干净退出。所以把导入结果用起来
        # （版本号进输出、QApplication 做个类型检查），而不是 import 完就丢。
        from PyQt5.QtCore import QT_VERSION_STR
        from PyQt5.QtWidgets import QApplication

        if not callable(getattr(QApplication, "instance", None)):
            raise RuntimeError("QApplication 不可用")
        return f"Qt {QT_VERSION_STR}，插件目录 {root}"

    def _focus():
        from danmaku_kb.input.window import (
            WINDOWS_API,
            WindowInfo,
            get_foreground_window,
            list_windows,
            window_matches,
        )

        info = WindowInfo(
            hwnd=1, title="自检窗口", class_name="SelfTestWnd", process_name="selftest.exe"
        )
        # 逐条走一遍匹配语义；任何一条不对，用户都会遇到"填了目标却永远不生效"
        if not window_matches(info, "selftest", field="process"):
            raise AssertionError("进程名包含匹配失败")
        if not window_matches(info, "SELFTEST.exe", field="process", mode="exact"):
            raise AssertionError("进程名精确匹配应忽略 .exe 与大小写")
        if not window_matches(info, r"SelfTest\w+", field="class", mode="regex"):
            raise AssertionError("类名正则匹配失败")
        if window_matches(info, "([", mode="regex"):
            raise AssertionError("非法正则必须按不匹配处理（fail closed）")
        if window_matches(info, "   "):
            raise AssertionError("空目标不能匹配一切")
        if window_matches(None, "selftest"):
            raise AssertionError("取不到窗口时必须按不匹配处理")
        if not WINDOWS_API:
            return "匹配逻辑正常（非 Windows，跳过窗口探测）"
        # 真的探一次本机窗口：这是唯一能证明"用户机器上读得到窗口"的检查
        count = len(list_windows(limit=200))
        fg = get_foreground_window()
        return f"匹配逻辑正常；可见窗口 {count} 个，当前前台 {fg.label if fg else '读不到'}"

    check("模块导入", _imports)
    check("默认映射", _mappings)
    check("弹幕匹配", _match)
    check("热键归一化", _hotkey)
    check("筛选引擎", _filter)
    check("protobuf 解析", _protobuf)
    check("引擎端到端", _engine)
    check("二维码渲染", _qr)
    check("焦点窗口", _focus)

    # 配置读写需要一次性临时目录。这里有两个**不能**用的写法，都是实测踩出来的：
    #   * tempfile.mkdtemp() —— 它在受限环境里建出 0o700 目录，而这种权限组合
    #     恰好不可访问（写不进、删不掉，见 docs/架构.md「测试基础设施」两条陷阱），
    #     实测会让自检**卡死**在写文件那一步；
    #   * tempfile.TemporaryDirectory() —— 收尾时 chmod 会挂住。
    # 所以仍然用"日志目录旁边"这个本身就可写的位置，但换唯一名字并尽力清理：
    # 老实现固定叫 selftest 且从不清理，在项目根目录跑一次就会留下一个
    # selftest\settings.json（还会被误当成项目文件）。
    import shutil
    import uuid

    from danmaku_kb.utils.logging_setup import get_log_dir

    probe_dir = None
    try:
        probe_dir = get_log_dir().parent / f"selftest_{uuid.uuid4().hex[:8]}"
        probe_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        probe_dir = None
    if probe_dir is not None:
        try:
            check("配置读写", lambda: _config(str(probe_dir)))
        finally:
            # 尽力清理；删不掉也不影响自检结论（Windows 上偶发占用）。
            shutil.rmtree(probe_dir, ignore_errors=True)
    else:
        print("  [SKIP] 配置读写 —— 无法创建临时目录")

    check("Qt 环境", _gui)

    print()
    if failures:
        print(f"自检失败 {len(failures)} 项：")
        for name, exc in failures:
            print(f"  - {name}: {exc}")
        return EXIT_SELFTEST
    print("全部通过。")
    return EXIT_OK


def run_check_env() -> int:
    """``--check-env``：把"按键为什么发不出去"的判据一次性打出来。

    与 ``--selftest`` 分工不同：自检验的是**代码链路**对不对（离线、必过），
    这里查的是**本机当前环境**的取值——是否受限令牌、是否被沙箱最高权限
    限制、目标游戏是否提权、配置目录到底写不写得进去。这几项任一不对，
    按键就会"发出去了但对方收不到"，而日志里没有任何失败痕迹。
    """

    print(f"弹幕键盘 V2 {__version__} 注入环境检查")

    from danmaku_kb.config.manager import ConfigManager, default_config_dir, writable_config_dir
    from danmaku_kb.input.keycodes import (
        WINDOWS_API,
        injection_environment_problem,
        injection_loopback,
        restricted_host_markers,
        target_blocks_injection,
    )
    from danmaku_kb.input.window import get_foreground_window

    print(f"  进程 PID        : {os.getpid()}")
    print(f"  Win32 注入 API  : {'可用' if WINDOWS_API else '不可用（非 Windows 平台）'}")

    try:
        import ctypes

        elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception as exc:  # pragma: no cover - 非 Windows
        elevated = False
        print(f"  管理员权限      : 无法判断（{type(exc).__name__}: {exc}）")
    else:
        print(f"  管理员权限      : {'是（已提权）' if elevated else '否（普通权限）'}")

    problem = injection_environment_problem()
    if problem:
        print("  注入环境        : 有问题")
        print(f"      {problem}")
    else:
        print("  注入环境        : 看起来正常（SendInput 未被本进程令牌限制）")

    markers = restricted_host_markers()
    if markers:
        print(f"  受限宿主标记    : {markers}")
        print(
            "      说明：本程序是由这个宿主启动的。宿主可能限制输入注入；"
            "真实判据看下面的「注入回环」。"
        )
    else:
        print("  受限宿主标记    : 无")

    if WINDOWS_API:
        loop_ok, loop_text = injection_loopback()
        print(f"  注入回环        : {'有效' if loop_ok else '**无效**'}")
        print(f"      {loop_text}")
    else:
        print("  注入回环        : 跳过（非 Windows）")

    foreground = get_foreground_window()
    if foreground is None:
        print("  当前前台窗口    : 读不到（非 Windows 或没有前台窗口）")
    else:
        print(f"  当前前台窗口    : {foreground.label}（pid {foreground.pid}）")
        blocked = target_blocks_injection(foreground.pid)
        if blocked:
            print("      目标窗口      : 有问题")
            print(f"      {blocked}")
        else:
            print("      目标窗口      : 权限不高于本程序，按键可以送达")

    preferred = default_config_dir()
    actual = writable_config_dir()
    print(f"  默认配置目录    : {preferred}")
    print(f"  实际使用目录    : {actual}")
    if actual != preferred:
        print(
            "      说明：默认目录不可写，已自动改用上面这个。"
            "设置会存在那里，不会丢，但不是你预期的位置。"
        )
    try:
        manager = ConfigManager(auto_save=False)
        if manager.ensure_dir() and manager.save():
            print("      写入测试      : 通过")
        else:
            print("      写入测试      : 失败（设置无法保存）")
    except Exception as exc:
        print(f"      写入测试      : 失败（{type(exc).__name__}: {exc}）")

    print()
    print("说明：若上面提示“注入环境/目标窗口有问题”或「注入回环 **无效**」，")
    print("      按键不会生效——这属于 Windows 的权限隔离或受限宿主，不是程序缺陷。")
    print("      ① 在本机受限宿主（沙箱/自动化宿主）里运行时，注入会被静默丢弃：")
    print("         请在 Windows 资源管理器里双击「启动.bat」或 exe 运行。")
    print("      ② 游戏与本程序必须处于同一权限级别（都普通启动，或都提权）。")
    return EXIT_OK


def main(argv=None) -> int:
    _forgiving_std_streams()
    args = parse_args(argv)

    if args.log_dir:
        os.environ["DANMAKU_KB_LOG_DIR"] = args.log_dir
    if args.config_dir:
        os.environ["DANMAKU_KB_CONFIG_DIR"] = args.config_dir

    import logging

    log_path = setup_logging(level=logging.DEBUG if args.debug else logging.INFO)

    if args.selftest:
        print(f"日志：{log_path}")
        return run_selftest()

    if args.check_env:
        print(f"日志：{log_path}")
        return run_check_env()

    # 界面必须在 qt_env 之后再导入（ui/__init__ 已保证顺序）
    from danmaku_kb.ui.qt_env import PYQT5_AVAILABLE, configure_qt_environment

    if not PYQT5_AVAILABLE:
        print(
            "缺少 PyQt5，无法启动界面。\n"
            "请先安装依赖：pip install -r requirements.txt\n"
            "或运行自检：python main.py --selftest",
            file=sys.stderr,
        )
        return EXIT_ENV
    configure_qt_environment()

    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication

    from danmaku_kb.config.manager import ConfigManager
    from danmaku_kb.ui.main_window import MainWindow
    from danmaku_kb.ui.theme import STYLESHEET

    # 高分屏：Qt5 默认不按系统缩放渲染，在 125%/150% 的屏幕上界面会糊。
    # 必须在 QApplication 实例化**之前**设置这个属性；qt_env 里设的
    # QT_AUTO_SCREEN_SCALE_FACTOR 只是环境变量层面的兜底。
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    # argv 只留程序名：自己的 --debug/--platform 等参数已经解析过了，
    # 传给 Qt 只会让它去理解一堆它不认识的选项。
    app = QApplication([sys.argv[0]])
    app.setApplicationName("弹幕键盘 V2")
    app.setStyleSheet(STYLESHEET)

    manager = ConfigManager()
    manager.load()

    window = MainWindow(config=manager.config, config_manager=manager)
    window.show()

    if args.platform:
        from danmaku_kb.config.models import Platform

        idx = window.platform_combo.findData(Platform.coerce(args.platform))
        if idx >= 0:
            window.platform_combo.setCurrentIndex(idx)
    if args.room:
        window.room_edit.setText(args.room)
    if args.no_hotkey and window.hotkey is not None:
        window.hotkey.stop()
        window.hotkey = None

    if args.simulate:
        window.sim_check.setChecked(True)
        window._on_connect()

    window.log(f"日志文件：{log_path}")
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
