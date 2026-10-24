# -*- coding: utf-8 -*-
"""引擎 → Qt 信号桥（``ui/worker.py``）与日志基础设施（``utils/logging_setup.py``）的测试。

这两个模块此前也是零覆盖：
- ``EngineBridge`` 是抓取线程与 Qt 主线程之间**唯一**的安全通道，
  参考项目直接在抓取线程里改控件，靠"碰巧没崩"运行。
- ``logging_setup`` 修的是参考项目"两个模块各自 ``basicConfig`` 后者被静默忽略"
  的坑，它一旦回归，抖音模块的日志会再次整段消失。

``EngineBridge`` 需要 PyQt5；缺了就跳过那一组。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

import pytest

from screenkey.engine import ScreenKeyEngine, EngineEvent, EngineStats
from screenkey.danmaku.message import DanmakuMessage, MessageType


# ----------------------------------------------------------------------
# EngineBridge
# ----------------------------------------------------------------------


@pytest.fixture
def qt_bridge():
    """离屏 QApplication + 一个挂好 EngineBridge 的引擎。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5", reason="需要 PyQt5 才能测信号桥")

    from PyQt5.QtWidgets import QApplication

    from screenkey.ui.qt_env import configure_qt_environment

    configure_qt_environment()
    app = QApplication.instance() or QApplication([])

    from screenkey.ui.worker import EngineBridge

    engine = ScreenKeyEngine()
    engine.dry_run = True
    bridge = EngineBridge(engine)
    return app, engine, bridge


def _chat(content="测试"):
    return DanmakuMessage(content=content, nickname="用户", user_id=1,
                          msg_type=MessageType.DANMAKU, platform="simulator")


class TestEngineBridgeSignals:
    def test_bridge_attaches_itself_as_engine_callback(self, qt_bridge):
        _, engine, bridge = qt_bridge
        assert engine.on_event is not None
        assert engine.on_event.__self__ is bridge

    def test_message_event_emits_message_received(self, qt_bridge):
        _, _, bridge = qt_bridge
        got: list = []
        bridge.messageReceived.connect(lambda m, a, t: got.append((m, a, t)))

        bridge._on_event(EngineEvent(kind="message", message=_chat("你好"), actionable=False))

        assert len(got) == 1
        message, actionable, text = got[0]
        assert message.content == "你好"
        assert actionable is False, "普通消息不应标成触发了按键"

    def test_action_event_emits_both_signals(self, qt_bridge):
        _, _, bridge = qt_bridge
        messages: list = []
        actions: list = []
        bridge.messageReceived.connect(lambda m, a, t: messages.append((m, a, t)))
        bridge.actionTriggered.connect(lambda m, ms: actions.append((m, ms)))

        bridge._on_event(EngineEvent(kind="action", message=_chat("w"),
                                     actionable=True, text="w 点按", matches=[]))

        assert messages[0][1] is True, "触发了按键的消息 actionable 应为 True"
        assert messages[0][2] == "w 点按"
        assert len(actions) == 1

    def test_action_event_without_message_does_not_emit_message_received(self, qt_bridge):
        """``action`` 事件可能没有 message；此时不应发 messageReceived。"""
        _, _, bridge = qt_bridge
        messages: list = []
        actions: list = []
        bridge.messageReceived.connect(lambda *a: messages.append(a))
        bridge.actionTriggered.connect(lambda *a: actions.append(a))

        bridge._on_event(EngineEvent(kind="action", message=None, actionable=True))

        assert messages == []
        assert len(actions) == 1

    def test_message_event_without_message_is_ignored(self, qt_bridge):
        _, _, bridge = qt_bridge
        got: list = []
        bridge.messageReceived.connect(lambda *a: got.append(a))
        bridge._on_event(EngineEvent(kind="message", message=None))
        assert got == []

    def test_status_event_reads_connected_from_extra(self, qt_bridge):
        """连接标志在 ``extra["connected"]`` 里，**不是** ``actionable``。

        这是引擎与界面之间的隐式契约，写错会让界面永远显示"未连接"。
        """
        _, _, bridge = qt_bridge
        got: list = []
        bridge.statusChanged.connect(lambda c, t: got.append((c, t)))

        bridge._on_event(EngineEvent(kind="status", actionable=True,
                                     text="已连接", extra={"connected": True}))
        bridge._on_event(EngineEvent(kind="status", actionable=False,
                                     text="已断开", extra={"connected": False}))

        assert got == [(True, "已连接"), (False, "已断开")]

    def test_status_event_defaults_to_disconnected(self, qt_bridge):
        _, _, bridge = qt_bridge
        got: list = []
        bridge.statusChanged.connect(lambda c, t: got.append((c, t)))
        bridge._on_event(EngineEvent(kind="status", text="状态未知"))
        assert got == [(False, "状态未知")]

    def test_error_event(self, qt_bridge):
        _, _, bridge = qt_bridge
        got: list = []
        bridge.errorOccurred.connect(got.append)
        bridge._on_event(EngineEvent(kind="error", text="连接失败"))
        assert got == ["连接失败"]

    def test_error_event_with_empty_text_uses_placeholder(self, qt_bridge):
        _, _, bridge = qt_bridge
        got: list = []
        bridge.errorOccurred.connect(got.append)
        bridge._on_event(EngineEvent(kind="error", text=""))
        assert got == ["未知错误"], "空错误信息会让界面显示一个空白错误框"

    def test_stats_event_emits_stats_object(self, qt_bridge):
        _, _, bridge = qt_bridge
        got: list = []
        bridge.statsUpdated.connect(got.append)
        stats = EngineStats(total_messages=7)
        bridge._on_event(EngineEvent(kind="stats", message=stats))
        assert got == [stats]

    def test_stats_event_without_stats_object_is_ignored(self, qt_bridge):
        """message 是普通弹幕（没有 total_messages）时不该当统计发出去。"""
        _, _, bridge = qt_bridge
        got: list = []
        bridge.statsUpdated.connect(got.append)
        bridge._on_event(EngineEvent(kind="stats", message=_chat()))
        assert got == []

    def test_unknown_kind_is_harmless(self, qt_bridge):
        _, _, bridge = qt_bridge
        bridge._on_event(EngineEvent(kind="完全没见过的类型", text="x"))

    def test_on_event_has_its_own_guard(self, qt_bridge):
        """``_on_event`` 必须有 try/except，防的是**它自己**的逻辑出错。

        注意这个守卫**保护不了**槽函数抛异常——PyQt5 遇到槽里的未捕获异常会
        直接 ``abort()``，``emit()`` 根本不返回（见下面那条子进程测试）。
        它保护的是事件对象字段缺失/类型不对这类问题。
        """
        import ast
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "src" / "screenkey"
                  / "ui" / "worker.py")
        tree = ast.parse(source.read_text(encoding="utf-8"))
        bridge = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.ClassDef) and n.name == "EngineBridge")
        on_event = next(n for n in bridge.body
                        if isinstance(n, ast.FunctionDef) and n.name == "_on_event")
        assert any(isinstance(n, ast.Try) for n in ast.walk(on_event)), \
            "_on_event 没有 try/except，事件字段缺失会打断抓取线程"

    def test_on_event_survives_malformed_event(self, qt_bridge):
        """字段类型不对时不能抛回引擎（会打断抓取线程）。"""
        _, _, bridge = qt_bridge
        bridge._on_event(EngineEvent(kind="status", extra=None, text=None))
        bridge._on_event(EngineEvent(kind="error", text=None))


class TestQtAbortsOnSlotException:
    """PyQt5 的槽函数抛异常会 **abort 整个进程**，不是普通 Python 异常。

    这是一个必须写进文档的行为：``EngineBridge._on_event`` 里那个 try/except
    拦不住它，因为 ``emit()`` 永远不返回。任何界面槽函数（``_on_message``
    / ``_on_status`` 等）一旦抛异常，程序会以退出码 ``0xC0000409`` 静默死掉，
    用户只看到窗口消失，日志里连一行错误都没有。

    用子进程取证，因为这个行为本身就是"进程被杀"，在主进程里测不了。
    """

    def test_slot_exception_kills_process_with_qt_abort_code(self):
        import subprocess
        import sys
        import textwrap

        project = Path(__file__).resolve().parents[1]
        script = textwrap.dedent(
            """
            import os, sys
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            sys.path.insert(0, r"{src}")
            from screenkey.ui.qt_env import configure_qt_environment
            configure_qt_environment()
            from PyQt5.QtCore import QObject, pyqtSignal
            from PyQt5.QtWidgets import QApplication
            app = QApplication.instance() or QApplication([])

            class Src(QObject):
                sig = pyqtSignal()

            src = Src()
            src.sig.connect(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            print("BEFORE_EMIT", flush=True)
            src.sig.emit()
            print("AFTER_EMIT", flush=True)
            """
        ).format(src=project / "src")

        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(project), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=120,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert "BEFORE_EMIT" in proc.stdout, f"脚本没跑到 emit：{proc.stdout!r} {proc.stderr!r}"
        assert "AFTER_EMIT" not in proc.stdout, (
            "emit() 竟然返回了 —— 那说明这个 PyQt5 版本不 abort，"
            "本测试的前提变了，需要重新评估 _on_event 的守卫范围"
        )
        assert proc.returncode != 0, "槽抛异常后进程竟然正常退出"
        # Windows 上是 0xC0000409 (STATUS_STACK_BUFFER_OVERRUN)，即 Qt 的 qFatal 路径
        assert proc.returncode in (-1073740791, 3221226505, 1), (
            f"退出码 {proc.returncode} 与预期的 Qt abort 不符"
        )


class TestEngineBridgeDetach:
    def test_detach_clears_engine_callback(self, qt_bridge):
        _, engine, bridge = qt_bridge
        bridge.detach()
        assert engine.on_event is None, "关窗后引擎仍持有桥，回调会打到已销毁的对象上"

    def test_detach_is_idempotent(self, qt_bridge):
        _, _, bridge = qt_bridge
        bridge.detach()
        bridge.detach()

    def test_engine_really_calls_bridge_through_pipeline(self, qt_bridge):
        """端到端：走 ``engine.process``，桥必须收到信号。

        这条把"引擎发事件"与"桥转信号"两段接起来测，避免两边各自测通、
        中间的事件名对不上（例如引擎发 ``"msg"``、桥只认 ``"message"``）。
        """
        app, engine, bridge = qt_bridge
        got: list = []
        bridge.messageReceived.connect(lambda m, a, t: got.append((m, a, t)))

        engine.process(_chat("666"))
        app.processEvents()

        assert got, "引擎处理了一条弹幕，桥却什么都没收到"
        assert got[0][0].content == "666"


# ----------------------------------------------------------------------
# logging_setup
# ----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _close_log_handlers():
    """每个测试结束后关掉所有文件 handler。

    没有这个夹具，`setup_logging` 装的 `RotatingFileHandler` 会**一直开着**
    （root 和 raw logger 各一个），把临时目录里的 `app.log` / `danmaku.log`
    钉住不放。`conftest.py` 的 `tmp_path` 覆盖会尽力清理，
    但 Windows 上"文件仍被打开"就是删不掉，于是 `.test_tmp/` 里会留下
    跑完测试也清不掉的目录。这是测试自己制造的资源泄漏，必须在测试里收掉。

    顺带把 `_configured` 复位，避免测试之间互相影响。
    """
    from screenkey.utils import logging_setup

    yield

    for logger in (logging.getLogger(),
                   logging.getLogger("screenkey.danmaku.raw")):
        for handler in list(logger.handlers):
            try:
                logger.removeHandler(handler)
                handler.close()
            except Exception:  # pragma: no cover - 关不掉不该让测试失败
                pass
    logging_setup._configured = False


@pytest.fixture
def isolated_logs(tmp_path, monkeypatch):
    """把日志目录引到临时目录，并强制重新配置。"""
    from screenkey.utils import logging_setup

    log_dir = tmp_path / "logs"
    monkeypatch.setenv("SCREENKEY_LOG_DIR", str(log_dir))
    monkeypatch.setattr(logging_setup, "_log_dir", None)
    monkeypatch.setattr(logging_setup, "_configured", False)
    return logging_setup, log_dir


class TestLogDir:
    def test_env_override_wins(self, isolated_logs):
        setup, log_dir = isolated_logs
        assert setup.get_log_dir() == log_dir
        assert log_dir.is_dir()

    def test_falls_back_to_temp_when_unwritable(self, tmp_path, monkeypatch):
        """用户目录不可写时必须退回临时目录，绝不能让程序起不来。"""
        from screenkey.utils import logging_setup

        monkeypatch.setenv("SCREENKEY_LOG_DIR", str(tmp_path / "x"))
        monkeypatch.setattr(logging_setup, "_log_dir", None)

        real_mkdir = Path.mkdir

        def fake_mkdir(self, *a, **k):
            if "x" in self.parts:
                raise OSError("拒绝访问")
            return real_mkdir(self, *a, **k)

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)
        got = logging_setup.get_log_dir()
        assert got.name == "screenkey_logs", f"应退回临时目录，实际 {got}"

    def test_log_dir_is_cached(self, isolated_logs):
        setup, log_dir = isolated_logs
        assert setup.get_log_dir() is setup.get_log_dir()

    def test_falls_back_to_temp_when_home_is_unknown(self, monkeypatch):
        """``Path.home()`` 抛 ``RuntimeError`` 时也必须退回临时目录。

        ``Path.home()`` 在环境里没有 ``HOME`` / ``USERPROFILE`` 时抛的是
        ``RuntimeError``（不是 ``OSError``）。这种环境真实存在：精简环境变量
        启动的子进程、服务账号、CI 容器。曾经只捕 ``OSError``，于是
        ``RuntimeError: Could not determine home directory.`` 直接冒到顶层，
        ``main.py --selftest`` 变成空输出 + 退出码 1。
        """
        from screenkey.utils import logging_setup

        monkeypatch.delenv("SCREENKEY_LOG_DIR", raising=False)
        monkeypatch.setattr(logging_setup, "_log_dir", None)

        def boom():
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(logging_setup.Path, "home", staticmethod(boom), raising=False)
        got = logging_setup.get_log_dir()
        assert got.name == "screenkey_logs", f"应退回临时目录，实际 {got}"

    def test_probe_writable_detects_write_denied(self, tmp_path, monkeypatch):
        """**只 mkdir 成功不代表可写** —— 本模块踩过一次，代价是程序起不来。

        受限宿主里对已存在的目录 ``mkdir(exist_ok=True)`` 会成功，但创建文件
        被拒绝。旧实现只看 ``mkdir``，于是把这种目录判为可用，
        ``RotatingFileHandler`` 随后在 ``open()`` 抛 ``PermissionError``，
        程序在打印任何东西之前就退出（退出码 1、无输出、无日志）。
        """
        from screenkey.utils import logging_setup

        target = tmp_path / "logs"

        def denied(self, *a, **k):
            raise PermissionError(13, "Permission denied", str(self))

        monkeypatch.setattr(Path, "write_text", denied)
        assert logging_setup._probe_writable(target) is False

    def test_skips_candidate_that_is_not_writable(self, tmp_path, monkeypatch):
        """首选目录写不进去时必须继续找下一个，而不是硬着头皮用它。"""
        from screenkey.utils import logging_setup

        monkeypatch.delenv("SCREENKEY_LOG_DIR", raising=False)
        monkeypatch.setattr(logging_setup, "_log_dir", None)
        first = tmp_path / "first"
        second = tmp_path / "second"
        monkeypatch.setattr(logging_setup, "_default_log_dir", lambda: first)
        monkeypatch.setattr(logging_setup, "_portable_log_dir", lambda: second)
        probed: list = []

        def fake_probe(path):
            probed.append(path)
            return path != first

        monkeypatch.setattr(logging_setup, "_probe_writable", fake_probe)
        assert logging_setup.get_log_dir() == second
        assert probed == [first, second], "必须按顺序探测，跳过不可写的那个"

    def test_falls_back_when_every_candidate_is_unwritable(self, tmp_path, monkeypatch):
        """所有候选都不可写时退到临时目录，并且**不能抛**。"""
        from screenkey.utils import logging_setup

        monkeypatch.delenv("SCREENKEY_LOG_DIR", raising=False)
        monkeypatch.setattr(logging_setup, "_log_dir", None)
        monkeypatch.setattr(logging_setup, "_probe_writable", lambda path: False)
        got = logging_setup.get_log_dir()
        assert got.name == "screenkey_logs"

    def test_explicit_override_falls_back_without_probing_others(self, tmp_path, monkeypatch):
        """显式指定 ``SCREENKEY_LOG_DIR`` 时只试它自己，失败就退临时目录。"""
        from screenkey.utils import logging_setup

        monkeypatch.setenv("SCREENKEY_LOG_DIR", str(tmp_path / "chosen"))
        monkeypatch.setattr(logging_setup, "_log_dir", None)
        probed: list = []

        def fake_probe(path):
            probed.append(path)
            return False

        monkeypatch.setattr(logging_setup, "_probe_writable", fake_probe)
        got = logging_setup.get_log_dir()
        assert got.name == "screenkey_logs"
        assert len(probed) == 1, "显式指定的目录不该再去试别的候选"


class TestSetupLoggingFailures:
    """日志文件打不开时程序必须照常跑 —— 日志失败绝不能连累程序。"""

    def test_setup_logging_survives_unwritable_log_file(self, isolated_logs, monkeypatch):
        setup, log_dir = isolated_logs

        def denied(path, level):
            raise PermissionError(13, "Permission denied", str(path))

        monkeypatch.setattr(setup, "_make_file_handler", denied)
        path = setup.setup_logging(force=True)  # 不该抛
        assert path == log_dir / "app.log"
        root = logging.getLogger()
        assert any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            for h in root.handlers
        ), "文件日志失败时应至少留下控制台 handler"

    def test_setup_logging_survives_unwritable_danmaku_log(self, isolated_logs, monkeypatch):
        """弹幕原文日志单独一个文件，它失败同样不能连累程序。"""
        setup, log_dir = isolated_logs
        real_make = setup._make_file_handler

        def partial(path, level):
            if str(path).endswith("danmaku.log"):
                raise PermissionError(13, "Permission denied", str(path))
            return real_make(path, level)

        monkeypatch.setattr(setup, "_make_file_handler", partial)
        path = setup.setup_logging(force=True)  # 不该抛
        assert path == log_dir / "app.log"
        assert (log_dir / "app.log").exists()
        raw = logging.getLogger("screenkey.danmaku.raw")
        assert raw.handlers == [], "造不出来就不该挂一个坏 handler"


class TestDefaultConfigDir:
    """``default_config_dir()`` 的 ``Path.home()`` 兜底。

    和日志目录是**同一个坑的另一处**：``Path.home()`` 缺 ``HOME`` /
    ``USERPROFILE`` 时抛 ``RuntimeError`` 而非 ``OSError``。这里不退回临时
    目录而是退回 CWD 下的 ``.screenkey`` —— 配置得让用户找得到。
    """

    def test_uses_env_override(self, monkeypatch, tmp_path):
        from screenkey.config.manager import ENV_CONFIG_DIR, default_config_dir

        monkeypatch.setenv(ENV_CONFIG_DIR, str(tmp_path / "envcfg"))
        assert default_config_dir() == tmp_path / "envcfg"

    def test_falls_back_to_cwd_when_home_is_unknown(self, monkeypatch):
        from screenkey.config import manager as manager_mod

        monkeypatch.delenv(manager_mod.ENV_CONFIG_DIR, raising=False)

        def boom():
            raise RuntimeError("Could not determine home directory.")

        monkeypatch.setattr(manager_mod.Path, "home", staticmethod(boom), raising=False)
        got = manager_mod.default_config_dir()
        assert got == manager_mod.Path.cwd() / manager_mod.DEFAULT_DIR_NAME, got

    def test_normal_path_is_home_dir(self, monkeypatch, tmp_path):
        from screenkey.config import manager as manager_mod

        monkeypatch.delenv(manager_mod.ENV_CONFIG_DIR, raising=False)
        monkeypatch.setattr(
            manager_mod.Path, "home", staticmethod(lambda: tmp_path), raising=False
        )
        assert manager_mod.default_config_dir() == tmp_path / manager_mod.DEFAULT_DIR_NAME


class TestSetupLogging:
    def test_returns_path_and_creates_file_on_first_log(self, isolated_logs):
        setup, log_dir = isolated_logs
        path = setup.setup_logging(force=True)
        assert path == log_dir / "app.log"
        setup.get_logger("t").info("hello")
        for handler in logging.getLogger().handlers:
            handler.flush()
        assert path.exists()

    def test_is_idempotent(self, isolated_logs):
        """重复调用不得叠加 handler（否则同一行日志打印 N 遍）。"""
        setup, _ = isolated_logs
        setup.setup_logging(force=True)
        first = len(logging.getLogger().handlers)
        setup.setup_logging()
        setup.setup_logging()
        assert len(logging.getLogger().handlers) == first, "重复调用叠加了 handler"

    def test_force_reconfigures(self, isolated_logs):
        setup, _ = isolated_logs
        setup.setup_logging(force=True)
        setup.setup_logging(force=True)
        assert logging.getLogger().level == logging.DEBUG

    def test_console_can_be_disabled(self, isolated_logs):
        setup, _ = isolated_logs
        setup.setup_logging(console=False, force=True)
        streams = [h for h in logging.getLogger().handlers
                   if isinstance(h, logging.StreamHandler)
                   and not isinstance(h, logging.handlers.RotatingFileHandler)]
        assert streams == []

    def test_danmaku_raw_logger_is_separate(self, isolated_logs):
        """弹幕原文走独立 logger + 独立文件，方便"只看弹幕"。

        参考项目的缺陷是两个模块各自 ``basicConfig``，第二次被静默忽略，
        导致抖音的日志全丢。这里确保两个 logger 各有各的 handler。
        """
        setup, log_dir = isolated_logs
        setup.setup_logging(force=True)
        raw = setup.get_raw_danmaku_logger()
        assert raw.name == "screenkey.danmaku.raw"
        assert raw.propagate is False, "原文 logger 不应向 root 冒泡（会写两遍）"
        assert raw.handlers, "原文 logger 没有自己的 handler"

        raw.info("一条弹幕原文")
        for handler in raw.handlers:
            handler.flush()
        assert (log_dir / "danmaku.log").exists()

    def test_noisy_libraries_are_quieted(self, isolated_logs):
        setup, _ = isolated_logs
        setup.setup_logging(force=True)
        for name in ("websockets", "aiohttp", "asyncio", "urllib3", "blivedm"):
            assert logging.getLogger(name).level == logging.WARNING, f"{name} 没降噪"

    def test_rotating_handler_limits_file_size(self, isolated_logs):
        setup, _ = isolated_logs
        setup.setup_logging(force=True)
        rotating = [h for h in logging.getLogger().handlers
                    if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert rotating, "没有按大小轮转的 handler，日志会无限增长"
        assert rotating[0].maxBytes == setup.MAX_BYTES
        assert rotating[0].backupCount == setup.BACKUP_COUNT


class TestGetLogger:
    def test_adds_package_prefix(self):
        from screenkey.utils.logging_setup import get_logger

        assert get_logger("danmaku.douyin").name == "screenkey.danmaku.douyin"

    def test_does_not_double_prefix(self):
        from screenkey.utils.logging_setup import get_logger

        assert get_logger("screenkey.engine").name == "screenkey.engine"

    def test_dunder_name_works(self):
        """模块里普遍写 ``get_logger(__name__)``，``__name__`` 已是全名。"""
        from screenkey.utils.logging_setup import get_logger

        assert get_logger("screenkey.input.keyboard").name == "screenkey.input.keyboard"

    def test_same_name_returns_same_logger(self):
        from screenkey.utils.logging_setup import get_logger

        assert get_logger("a.b") is get_logger("a.b")

    def test_logger_is_usable_without_setup(self):
        """没调 ``setup_logging`` 也能取 logger 并记日志（不能抛）。"""
        from screenkey.utils.logging_setup import get_logger

        get_logger("never.configured").debug("没事")


class TestLoggingContract:
    def test_module_never_calls_basicconfig(self):
        """本模块是唯一配置 root 的地方，且**不用** ``basicConfig``。

        ``basicConfig`` 只在 root 无 handler 时生效，第二次调用被静默忽略——
        这正是参考项目抖音日志丢失的根因。用 AST 剥掉 docstring 后检查，
        因为 docstring 里**故意提到** ``basicConfig`` 来解释这个坑。
        """
        import ast

        source = Path(__file__).resolve().parents[1] / "src" / "screenkey" / "utils" / "logging_setup.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        # 剥掉所有 docstring（模块级 + 函数级 + 类级）
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = node.body
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    node.body = body[1:]
        code = ast.unparse(tree)
        assert "basicConfig" not in code, "用了 basicConfig：第二次调用会被静默忽略"

    def test_danmaku_raw_handler_follows_force_reconfigure(self, tmp_path, monkeypatch):
        """``force=True`` 后 raw logger 必须指向**新的**日志目录。

        历史缺陷：那段代码是 ``if not danmaku_logger.handlers:``，而 raw logger
        的 handler 在第一次配置后就一直存在，于是 ``force=True`` 时它被跳过，
        继续往**旧目录**写；root 的日志却已经写到新目录。结果 ``danmaku.log``
        与 ``app.log`` 分居两地，排查问题时看不到弹幕原文。
        """
        from screenkey.utils import logging_setup

        dir_a = tmp_path / "A"
        dir_b = tmp_path / "B"

        def configure(path):
            monkeypatch.setenv("SCREENKEY_LOG_DIR", str(path))
            monkeypatch.setattr(logging_setup, "_log_dir", None)
            monkeypatch.setattr(logging_setup, "_configured", False)
            logging_setup.setup_logging(force=True)
            raw = logging_setup.get_raw_danmaku_logger()
            return str(raw.handlers[0].baseFilename)

        configure(dir_a)
        second = configure(dir_b)

        assert second == str(dir_b / "danmaku.log"), (
            f"force 之后 raw handler 仍指向旧目录：{second}"
        )
        raw = logging_setup.get_raw_danmaku_logger()
        assert len(raw.handlers) == 1, "force 之后 raw logger 应只有一个 handler"

    def test_modules_that_log_use_get_logger(self):
        """打了日志的模块必须走统一的 ``get_logger``，不许自己 ``basicConfig``。

        这条守的是参考项目那个真实缺陷：两个弹幕模块**各自**调
        ``logging.basicConfig``，而 basicConfig 只在 root 没有 handler 时才生效，
        于是第二个模块的配置被静默忽略、它的日志落到了别处。

        判据分三条，都基于 AST（子串匹配会被 ``fingerprint(`` 这种名字骗到）：

        1. 凡调用 ``get_logger(`` 的模块，其 logger 必须来自这个工厂——
           即模块里不得出现 ``logging.getLogger(``（``logging_setup`` 自己除外）。
        2. ``logging.basicConfig`` 只允许出现在 ``utils/logging_setup.py``，
           且只允许出现在注释/文档字符串里（真正调用它的地方有且只有一处，
           就是 ``setup_logging`` 内部那个，这里用 AST 只查调用表达式）。
        3. 覆盖面不能退化成空跑：至少要有 15 个模块在用 ``get_logger``。
        """
        import ast

        src = Path(__file__).resolve().parents[1] / "src" / "screenkey"
        users = []
        for path in sorted(src.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(src).as_posix()
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)

            if "get_logger(" in text:
                users.append(rel)
                if rel != "utils/logging_setup.py":
                    assert "logging.getLogger(" not in text, (
                        f"{rel} 绕过了 get_logger 自己取 logger"
                    )

            if rel == "utils/logging_setup.py":
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "basicConfig"):
                    raise AssertionError(
                        f"{rel} 自己调了 logging.basicConfig（会与统一配置打架）"
                    )

        assert len(users) >= 15, f"只有 {len(users)} 个模块用 get_logger，覆盖面退化了"

    def test_pure_modules_stay_pure(self):
        """``filters/rules.py`` 与 ``utils/helpers.py`` 是纯逻辑，不该引入 logging。

        这条是**故意**钉住的：它们能在测试里被无副作用地反复调用，
        正是因为没有 I/O。将来有人往里加日志，说明职责变了，应当被注意到。
        """
        src = Path(__file__).resolve().parents[1] / "src" / "screenkey"
        for rel in ("filters/rules.py", "utils/helpers.py"):
            text = (src / rel).read_text(encoding="utf-8")
            assert "import logging" not in text, f"{rel} 变成了带 I/O 的模块"
