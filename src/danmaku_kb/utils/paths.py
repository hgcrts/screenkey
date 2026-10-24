# -*- coding: utf-8 -*-
"""目录可写性探测。

这个项目在受限宿主里踩过两次同一个坑，所以逻辑只留一份：

``mkdir(parents=True, exist_ok=True)`` 对**已存在**的目录会直接成功（不抛异常），
但往里写文件仍可能被拒绝（受限令牌、安全软件、只读的漫游目录都是这样）。
只凭 ``mkdir`` 就把目录判为可用，紧接着 ``RotatingFileHandler`` 在 ``open()``
时抛 ``PermissionError``，程序在打印任何东西之前就退出——退出码 1、无输出、
连日志都没有。这是最糟糕的故障模式：用户双击后"什么都没发生"。

所以只有**真写进一个文件再删掉**才算可写。
"""

from __future__ import annotations

from pathlib import Path

#: 探测用文件名；用完立刻删掉
PROBE_NAME = ".write_test"


def probe_writable(path: Path) -> bool:
    """真的往目录里写一个文件再删掉；只 ``mkdir`` 成功**不算**可写。"""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    probe = Path(path) / PROBE_NAME
    try:
        probe.write_text("x", encoding="utf-8")
        return True
    except OSError:
        return False
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


__all__ = ["PROBE_NAME", "probe_writable"]
