"""抓取器生命周期的回归测试。

这一层专门守一个**真实且静默**的缺陷类别：**实例属性遮蔽同名方法**。

背景
----
``DanmakuFetcher.__init__`` 曾经把事件循环存进 ``self._loop``，
而 ``SimulatorFetcher`` 恰好把主循环方法命名为 ``async def _loop()``。
Python 里**实例属性优先于类里的方法**，于是 ``self._loop`` 拿到的是
``ProactorEventLoop`` 对象，``await self._loop()`` 抛::

    TypeError: 'ProactorEventLoop' object is not callable

这个异常被 ``_guarded_loop`` 的 ``except Exception`` 吞成一条
"模拟器异常"日志，**没有让任何测试失败**——因为此前没有测试真的走
``start()`` 那条线程路径，大家都只调 ``make_message()`` / ``emit_once()``。

修法有两处，这里都要守住：
1. 基类属性改名 ``_event_loop``（``base.py``）；
2. 子类方法改名 ``_emit_loop``（``simulator.py``），双保险。
"""

from __future__ import annotations

import ast
import inspect
import time
from pathlib import Path

import pytest

from screenkey.danmaku.simulator import SimulatorFetcher

PKG = Path(__file__).resolve().parent.parent / "src" / "screenkey"
FETCHER_DIR = PKG / "danmaku"


def _self_attrs(cls_node: ast.ClassDef) -> set[str]:
    """类里所有 ``self.xxx = ...`` 形式的实例属性名。"""
    out: set[str] = set()
    for node in ast.walk(cls_node):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                out.add(target.attr)
    return out


def _class_nodes(path: Path) -> list[ast.ClassDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]


def _methods(cls_node: ast.ClassDef) -> set[str]:
    return {
        item.name
        for item in cls_node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


class TestNoAttributeShadowsMethod:
    """任何抓取器子类都不得让方法名撞上基类实例属性名。"""

    def test_base_uses_event_loop_not_loop(self):
        """基类实例属性必须是 ``_event_loop``，不能是裸的 ``_loop``。"""

        base_cls = next(
            c for c in _class_nodes(FETCHER_DIR / "base.py") if c.name == "DanmakuFetcher"
        )
        attrs = _self_attrs(base_cls)
        assert "_event_loop" in attrs
        assert "_loop" not in attrs, (
            "基类又把事件循环存回 self._loop 了 —— 这会遮蔽子类的同名方法，"
            "见本模块 docstring"
        )

    @pytest.mark.parametrize(
        "filename",
        ["base.py", "simulator.py", "bilibili.py", "douyin.py"],
    )
    def test_no_subclass_method_shadowed_by_base_attr(self, filename: str):
        """AST 级扫描：子类方法名 ∩ 基类实例属性名 必须为空。"""

        base_cls = next(
            c for c in _class_nodes(FETCHER_DIR / "base.py") if c.name == "DanmakuFetcher"
        )
        base_attrs = _self_attrs(base_cls)

        path = FETCHER_DIR / filename
        for cls in _class_nodes(path):
            if cls.name == "DanmakuFetcher":
                continue
            clash = sorted(base_attrs & _methods(cls))
            assert not clash, (
                f"{path.name}::{cls.name} 的方法 {clash} 撞上了基类实例属性 "
                f"{sorted(base_attrs)} —— 实例属性会遮蔽方法，调用点会抛 "
                f"TypeError，而且可能被宽 except 吞掉"
            )


class TestSimulatorRealStartPath:
    """真的走一遍 ``start()``：起后台线程、真事件循环、真产出弹幕。

    这是上个缺陷唯一能被抓到的路径，此前完全没有覆盖。
    """

    def test_start_actually_emits_messages(self):
        """``start()`` 之后必须真的收到弹幕，而不是只留下一条异常日志。"""

        fetcher = SimulatorFetcher(interval=0.01)
        received: list[str] = []
        errors: list[str] = []
        fetcher.on_message(lambda m: received.append(m.content))
        fetcher.on_error(errors.append)

        assert fetcher.start("simulator") is True
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and len(received) < 5:
                time.sleep(0.02)
        finally:
            fetcher.stop()
            fetcher.join(timeout=3.0)

        assert not errors, f"抓取器报了错: {errors}"
        assert len(received) >= 5, f"5 秒内只收到 {len(received)} 条弹幕"
        assert fetcher.message_count >= 5

    def test_loop_method_is_callable_coroutine(self):
        """主循环方法必须真的是可等待的协程函数。"""

        import asyncio

        method = getattr(SimulatorFetcher, "_emit_loop")
        assert asyncio.iscoroutinefunction(method), "_emit_loop 必须是协程函数"

        # 双保险：实例上取出来的也必须是绑定的协程方法，
        # 而不是被某个属性盖成了事件循环对象。
        fetcher = SimulatorFetcher()
        bound = getattr(fetcher, "_emit_loop")
        assert asyncio.iscoroutinefunction(bound)


class TestFetcherLifecycleContract:
    """基类声明的契约，用真实对象验一遍。"""

    def test_connect_returns_before_loop_finishes(self):
        """``connect()`` 必须尽快返回，不能把整个抓取生命周期堵在里面。"""

        source = inspect.getsource(SimulatorFetcher.connect)
        # 不能出现 ``await self._emit_loop()`` / ``await self._guarded_loop()``
        assert "_guarded_loop()" in source
        assert "await self._guarded_loop" not in source
        assert "await self._emit_loop" not in source

    def test_stop_is_synchronous_and_join_finishes(self):
        """``stop()`` 不阻塞；``join()`` 之后线程必须真的退出。"""

        fetcher = SimulatorFetcher(interval=0.01)
        fetcher.start("simulator")

        started = time.monotonic()
        fetcher.stop()
        elapsed = time.monotonic() - started
        assert elapsed < 1.0, f"stop() 阻塞了 {elapsed:.2f}s，应立刻返回"

        assert fetcher.join(timeout=3.0) is True
        assert fetcher.running is False

    def test_callbacks_survive_exceptions(self):
        """一个回调抛异常，不得影响后续回调，也不得传出去。"""

        fetcher = SimulatorFetcher()
        seen: list[str] = []

        def boom(_message):
            raise RuntimeError("故意炸")

        fetcher.on_message(boom)
        fetcher.on_message(lambda m: seen.append(m.content))

        fetcher.emit_once("测试")  # 不得抛
        assert seen == ["测试"]
