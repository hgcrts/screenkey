"""回归测试：``DanmakuMessage.timestamp`` 必须是 ``datetime``，以及弹幕窗不能因它崩。

**这条缺陷真实发生过，并且让整个程序闪退**：``DanmakuMessage`` 声明
``timestamp: datetime``，但构造时并不规整，抖音/B站抓到的秒级、毫秒级整数或
ISO 字符串都能原样存进去。界面 ``DanmakuWindow.append`` 当时写的是
``datetime.fromtimestamp(message.timestamp or 0)``——对 ``datetime`` 调用会抛
``TypeError: 'datetime.datetime' object cannot be interpreted as an integer``；
而 **PyQt5 5.15 遇到槽里的未捕获异常会 qFatal 终结整个进程**
（``exit 0xC0000409``），所以症状是"模拟器发出第一条弹幕就闪退"。

此前 268 条测试全绿却完全没覆盖到：没有任何测试构造 ``DanmakuWindow``，
也没有测试断言 ``timestamp`` 的类型。

这里补两个层次：
1. 数据层——``timestamp`` 构造后恒为 ``datetime``（含 ``from_dict`` 路径）；
2. 界面层——真的建 ``DanmakuWindow`` 并调 ``append()``/``_save()``，确认不抛异常。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"


@pytest.fixture(scope="module")
def qt():
    """离屏 QApplication；没有 PyQt5 就跳过。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5", reason="需要 PyQt5 才能测界面")

    import sys

    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))

    from screenkey.ui.qt_env import PYQT5_AVAILABLE, configure_qt_environment

    assert PYQT5_AVAILABLE
    configure_qt_environment()

    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


# ===================================================================== 数据层
class TestTimestampIsAlwaysDatetime:
    """``timestamp`` 声明是 ``datetime``，那它就必须真的是 ``datetime``。"""

    @pytest.mark.parametrize(
        "label, raw",
        [
            ("datetime", datetime(2026, 10, 10, 19, 30, 0)),
            ("秒级 float", 1791000000.0),
            ("秒级 int", 1791000000),
            ("毫秒 int", 1791000000000),
            ("毫秒 float", 1791000000000.0),
            ("ISO 字符串", "2026-10-10T19:25:25"),
        ],
    )
    def test_constructor_normalizes(self, label, raw):
        from screenkey.danmaku.message import DanmakuMessage

        msg = DanmakuMessage(content="666", timestamp=raw)
        assert isinstance(msg.timestamp, datetime), f"{label} 没被规整成 datetime"

    @pytest.mark.parametrize("raw", [None, "", "not-a-time", "半夜"])
    def test_garbage_falls_back_to_datetime_not_crash(self, raw):
        """垃圾输入不抛异常，也不留下"非 datetime"的字段。"""
        from screenkey.danmaku.message import DanmakuMessage

        before = datetime.now()
        msg = DanmakuMessage(content="666", timestamp=raw)
        assert isinstance(msg.timestamp, datetime)
        # 回落应当是"此刻"，而不是 1970
        assert msg.timestamp.year >= before.year

    def test_omitted_timestamp_is_datetime(self):
        from screenkey.danmaku.message import DanmakuMessage

        assert isinstance(DanmakuMessage(content="666").timestamp, datetime)

    def test_milliseconds_are_divided_by_1000(self):
        """秒级与毫秒级同一个时刻必须得到同一个 datetime。

        这是"单位猜错"最容易出错的地方：毫秒值当秒用会跑到公元 58000 年。
        """
        from screenkey.danmaku.message import DanmakuMessage

        seconds = DanmakuMessage(content="x", timestamp=1791000000)
        millis = DanmakuMessage(content="x", timestamp=1791000000000)
        assert seconds.timestamp == millis.timestamp
        assert seconds.timestamp.year == 2026

    def test_system_message_timestamp_is_datetime(self):
        from screenkey.danmaku.message import make_system_message

        assert isinstance(make_system_message("已连接").timestamp, datetime)

    def test_evolved_and_with_content_keep_datetime(self):
        """``evolved``/``with_content`` 走 ``replace``，但 ``__post_init__`` 仍会跑。"""
        from screenkey.danmaku.message import DanmakuMessage

        msg = DanmakuMessage(content="666", timestamp=1791000000.0)
        assert isinstance(msg.evolved(content="777").timestamp, datetime)
        assert isinstance(msg.with_content("888").timestamp, datetime)

    def test_strftime_works_directly(self):
        """界面就是这么用的：直接对字段调 ``strftime``。

        修复前这里是 ``datetime.fromtimestamp(msg.timestamp or 0)``，
        对 datetime 会抛 TypeError。
        """
        from screenkey.danmaku.message import DanmakuMessage

        msg = DanmakuMessage(content="666")
        assert len(msg.timestamp.strftime("%H:%M:%S")) == 8
        assert len(msg.timestamp.strftime("%Y-%m-%d %H:%M:%S")) == 19

    def test_truthiness_bug_that_made_or_0_useless(self):
        """``or 0`` 兜底对 datetime 完全无效——这就是当时判断失误的根源。

        这条不是测我们自己的代码，而是把"为什么 ``or 0`` 救不了"钉在测试里，
        免得将来有人又觉得加个 ``or 0`` 就够了。
        """
        msg_dt = datetime(2000, 1, 1)
        assert bool(msg_dt) is True, "datetime 恒为真，所以 `timestamp or 0` 永远取左边"

    def test_frozen_still_enforced_after_post_init(self):
        """``__post_init__`` 用 ``object.__setattr__``，但不能破坏冻结语义。"""
        import dataclasses

        from screenkey.danmaku.message import DanmakuMessage

        msg = DanmakuMessage(content="666")
        with pytest.raises(dataclasses.FrozenInstanceError):
            msg.timestamp = datetime.now()  # type: ignore[misc]


class TestFromDictPreservesTimestamp:
    """``from_dict`` 曾经自己手写三分支，把**数字时间戳静默丢弃**换成"此刻"。"""

    def test_numeric_timestamp_survives(self):
        from screenkey.danmaku.message import DanmakuMessage

        msg = DanmakuMessage.from_dict({"content": "平台原始", "timestamp": 1791000000000})
        assert isinstance(msg.timestamp, datetime)
        assert msg.timestamp.year == 2026, "毫秒整数被丢弃换成了'此刻'"
        assert msg.timestamp == DanmakuMessage(content="x", timestamp=1791000000).timestamp

    def test_iso_string_survives(self):
        from screenkey.danmaku.message import DanmakuMessage

        msg = DanmakuMessage.from_dict({"content": "x", "timestamp": "2026-10-10T19:25:25"})
        assert msg.timestamp == datetime(2026, 10, 10, 19, 25, 25)

    def test_roundtrip_is_stable(self):
        from screenkey.danmaku.message import DanmakuMessage

        original = DanmakuMessage(content="hello", timestamp=1791000000.0)
        restored = DanmakuMessage.from_dict(original.to_dict())
        assert restored.timestamp == original.timestamp
        assert isinstance(restored.timestamp, datetime)

    def test_missing_timestamp_does_not_crash(self):
        from screenkey.danmaku.message import DanmakuMessage

        assert isinstance(DanmakuMessage.from_dict({"content": "x"}).timestamp, datetime)


# ===================================================================== 界面层
class TestDanmakuWindowDoesNotCrash:
    """真的建弹幕窗、真的走 ``append()``——这正是此前完全没被覆盖的路径。"""

    @pytest.fixture
    def window(self, qt):
        from screenkey.config.models import DisplayConfig
        from screenkey.ui.danmaku_window import DanmakuWindow

        win = DanmakuWindow(config=DisplayConfig())
        yield win
        win.close()

    def _msg(self, **kw):
        from screenkey.danmaku.message import DanmakuMessage

        return DanmakuMessage(content=kw.pop("content", "666"), **kw)

    def test_append_with_show_timestamp_enabled(self, window):
        """``show_timestamp`` 默认 True——默认配置下这条路径必崩。"""
        from screenkey.config.models import DisplayConfig

        assert DisplayConfig().show_timestamp is True, "默认必须开启才符合当时崩溃的现场"
        window.append(self._msg(timestamp=1791000000000), actionable=True, matched="6")

    @pytest.mark.parametrize(
        "raw", [datetime(2026, 1, 2, 3, 4, 5), 1791000000.0, 1791000000000, "2026-10-10T19:25:25", None]
    )
    def test_append_with_every_timestamp_flavour(self, window, raw):
        """无论上游塞进来什么，append 都不该抛。"""
        window.append(self._msg(timestamp=raw))

    def test_append_with_timestamp_disabled(self, window):
        from screenkey.config.models import DisplayConfig

        window.apply_config(DisplayConfig(show_timestamp=False))
        window.append(self._msg(timestamp=1791000000000))

    def test_append_system_works(self, window):
        window.append_system("已连接到模拟器")

    def test_flush_renders_queued_messages(self, window):
        """``_flush`` 才是真正写 DOM 的地方，异常同样发生在槽里。"""
        for i in range(5):
            window.append(self._msg(content=f"弹幕{i}", timestamp=1791000000000))
        window._flush()
        assert window.view.document().blockCount() > 0

    def test_export_writes_datetime_format(self, window, tmp_path, monkeypatch):
        """导出路径曾用同样的 ``fromtimestamp`` 写法，一并锁住。

        ``QFileDialog.getSaveFileName`` 在离屏下会阻塞，所以直接把它替换成
        返回临时文件路径。
        """
        from PyQt5.QtWidgets import QFileDialog

        target = tmp_path / "out.txt"
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), ""))
        )

        window.append(self._msg(content="第一条", timestamp=1791000000000))
        window.append(self._msg(content="第二条", timestamp=1791000000.0))
        window._save()

        assert target.exists(), f"没写出文件；状态栏是：{window.status.text()}"
        text = target.read_text(encoding="utf-8")
        assert "第一条" in text and "第二条" in text
        # 形如 [2026-10-03 12:00:00] [simulator] 测试用户: 第一条
        assert "[2026-" in text, text
        assert "1970" not in text, "时间戳没被正确处理"

    def test_close_stops_flush_timer(self, window):
        # 关闭后定时器不该继续跑（同 main_window 的 _stats_timer 一类问题）
        window.close()
        assert not window._flush_timer.isActive()

    def test_clear_resets_counters(self, window):
        window.append(self._msg())
        window.clear()
        assert window._count == 0
        assert len(window._history) == 0


class TestMainWindowMessagePathDoesNotCrash:
    """从 ``MainWindow._on_message`` 一路走到弹幕窗——真实崩溃发生的那条链。"""

    def test_on_message_with_numeric_timestamp(self, qt, tmp_path, monkeypatch):
        monkeypatch.setenv("SCREENKEY_CONFIG_DIR", str(tmp_path / "cfg"))

        from screenkey.config.manager import ConfigManager
        from screenkey.danmaku.message import DanmakuMessage
        from screenkey.ui.main_window import MainWindow

        manager = ConfigManager(config_dir=tmp_path / "cfg", auto_save=False)
        manager.load()
        window = MainWindow(config=manager.config, config_manager=manager)
        try:
            # 平台原始数据形态：毫秒整数时间戳
            window._on_message(DanmakuMessage(content="666666", timestamp=1791000000000), True, "6")
            window._on_message(DanmakuMessage(content="普通弹幕", timestamp=1791000000.0), False, "")
        finally:
            window.close()
