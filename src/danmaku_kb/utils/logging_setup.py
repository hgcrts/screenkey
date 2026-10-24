# -*- coding: utf-8 -*-
"""日志基础设施。

参考项目踩过的坑：``core/danmaku/bili.py`` 与 ``core/danmaku/douyin.py``
各自调用 ``logging.basicConfig()``，第二次调用被 Python 静默忽略
（``basicConfig`` 只在 root logger 没有 handler 时生效），导致抖音模块的
日志全部丢失，排查连接问题时看不到任何输出。

这里的规定：

- 全项目只在本模块配置 root logger 一次（幂等，重复调用无副作用）
- 每个模块用 :func:`get_logger` 取自己的具名 logger，不碰 root 配置
- 日志同时写文件与控制台；文件用 ``RotatingFileHandler`` 限长
- 弹幕原文单独走 ``danmaku`` logger，方便"只看弹幕"或"不看弹幕"
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional

from .paths import probe_writable as _probe_writable

#: 日志格式：时间 级别 模块名 消息
LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)-22s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 日志文件最大 5MB，保留 3 个备份
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

_configured = False
_log_dir: Optional[Path] = None


def _default_log_dir() -> Path:
    """``~/.danmaku_kb/logs``；连用户目录都问不出来时返回临时目录。

    ``Path.home()`` 在环境变量里没有 ``HOME`` / ``USERPROFILE`` 时会抛
    ``RuntimeError``（**不是** ``OSError``）。这种环境真实存在：精简环境变量
    启动的子进程、某些服务账号、CI 容器。因为一个日志目录就让程序起不来
    是荒唐的，所以这里也兜住 —— 之前只捕 ``OSError``，
    于是 ``RuntimeError: Could not determine home directory.``
    会直接冒到顶层，``--selftest`` 变成空输出 + 退出码 1。
    """
    try:
        return Path.home() / ".danmaku_kb" / "logs"
    except RuntimeError:
        import tempfile

        return Path(tempfile.gettempdir()) / "danmaku_kb_logs"


def _portable_log_dir() -> Path:
    """程序自身旁边的日志目录（打包后是 exe 同级）。

    用户目录不可写时的第一备选。放这里而不是临时目录，是因为日志是给用户
    **事后回看**的，得让他找得到、且重启不会被清掉。
    """
    if getattr(sys, "frozen", False):  # 打包后：exe 同级
        base = Path(sys.executable).resolve().parent
    else:  # utils/logging_setup.py -> utils -> danmaku_kb -> src -> 项目根
        base = Path(__file__).resolve().parents[3]
    return base / ".danmaku_kb" / "logs"


def _temp_log_dir() -> Path:
    """最后备选：系统临时目录。"""
    import tempfile

    return Path(tempfile.gettempdir()) / "danmaku_kb_logs"


def _warn_log_dir(message: str) -> None:
    """日志系统还没配好，只能直接写 stderr。"""
    try:
        sys.stderr.write(message + "\n")
    except Exception:  # pragma: no cover - stderr 都写不了就算了
        pass


def get_log_dir() -> Path:
    """返回日志目录（不存在则创建，并确保真的写得进去）。

    顺序：``DANMAKU_KB_LOG_DIR`` → ``~/.danmaku_kb/logs`` → 程序旁边的
    ``.danmaku_kb/logs`` → 系统临时目录。放在用户目录而不是项目目录，是为了
    让打包成 exe 后（只读安装目录）依然能写日志；用户目录不可写时再退到
    程序旁边——日志要让用户找得到，最后才轮到临时目录。
    """
    global _log_dir
    if _log_dir is not None:
        return _log_dir

    override = os.environ.get("DANMAKU_KB_LOG_DIR", "").strip()
    if override:
        # 显式指定的目录只试它自己；写不进去就如实退回临时目录并说明
        candidate = Path(override)
        if _probe_writable(candidate):
            _log_dir = candidate
        else:
            _warn_log_dir(
                f"警告：{candidate} 不可写，日志改用临时目录。"
                "（可用 DANMAKU_KB_LOG_DIR 指定一个可写目录。）"
            )
            fallback = _temp_log_dir()
            fallback.mkdir(parents=True, exist_ok=True)
            _log_dir = fallback
        return _log_dir

    for candidate in (_default_log_dir(), _portable_log_dir()):
        if _probe_writable(candidate):
            if candidate != _default_log_dir():
                _warn_log_dir(f"提示：用户目录不可写，日志已改写到 {candidate}")
            _log_dir = candidate
            return _log_dir

    fallback = _temp_log_dir()
    fallback.mkdir(parents=True, exist_ok=True)
    _warn_log_dir(f"提示：用户目录与程序目录都不可写，日志已改写到 {fallback}")
    _log_dir = fallback
    return _log_dir


def _make_file_handler(path: Path, level: int) -> logging.Handler:
    """构造一个按大小轮转的文件 handler。"""
    handler = logging.handlers.RotatingFileHandler(
        str(path),
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    return handler


def _try_make_file_handler(path: Path, level: int) -> Optional[logging.Handler]:
    """尽力造一个文件 handler；造不出来返回 ``None``，**绝不抛异常**。

    ``RotatingFileHandler`` 的构造函数会真的 ``open()`` 目标文件，失败的理由
    很现实：目录只读、磁盘满、**同一个日志文件正被另一个实例占用**（Windows
    上表现为 ``PermissionError``）。日志失败绝不能连累程序——所以这里兜住，
    降级成"本次只记控制台"。
    """
    try:
        return _make_file_handler(path, level)
    except Exception as exc:  # 含 OSError / PermissionError / ValueError
        _warn_log_dir(f"警告：无法写入日志文件 {path}（{exc}），本次不记录文件日志。")
        return None


def setup_logging(level: int = logging.INFO, console: bool = True, force: bool = False) -> Path:
    """配置全局日志。幂等；返回日志文件路径。

    :param level: 文件日志级别
    :param console: 是否同时输出到 stderr
    :param force: 强制重新配置（测试用）
    """
    global _configured

    log_dir = get_log_dir()
    log_file = log_dir / "app.log"

    if _configured and not force:
        return log_file

    root = logging.getLogger()
    # 先把新的文件 handler 造出来再拆旧的：万一造不出来（目录被改权限、
    # 磁盘满、日志文件被另一个实例锁着），至少不会出现"一个 handler 都不剩"
    # 的空窗，而且 —— 关键 —— 任何失败都不许冒出去把程序打死。
    root.setLevel(logging.DEBUG)
    file_handler = _try_make_file_handler(log_file, level)

    # 清掉可能由第三方库装上的 handler，避免同一行日志打印两遍
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # pragma: no cover - 关不掉也不该阻断配置
            pass

    if file_handler is not None:
        root.addHandler(file_handler)

    # 弹幕原文单独一个文件，便于事后回看"到底收到了什么"
    #
    # 注意这里**不能**写 ``if not danmaku_logger.handlers`` 就了事：
    # 那个条件在 ``force=True``（以及"上一次配置指向别的目录"）时同样为假，
    # 于是 raw logger 会一直抓着**第一次**配置时那个文件的 handler，
    # 日志继续往旧目录写，而 root 的日志已经写到新目录了。
    # 实测过：先配 A 再 force 配 B，raw handler 的 baseFilename 仍是 A。
    # 所以 force 时必须把旧的 handler 关掉重建。
    danmaku_logger = logging.getLogger("danmaku_kb.danmaku.raw")
    danmaku_logger.setLevel(logging.DEBUG)
    danmaku_logger.propagate = False
    if force:
        for handler in list(danmaku_logger.handlers):
            danmaku_logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover - 关不掉也不该阻断配置
                pass
    if not danmaku_logger.handlers:
        raw_handler = _try_make_file_handler(log_dir / "danmaku.log", logging.DEBUG)
        if raw_handler is not None:
            danmaku_logger.addHandler(raw_handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(logging.WARNING if level > logging.DEBUG else logging.DEBUG)
        stream.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        root.addHandler(stream)

    # 第三方库降噪
    for noisy in ("websockets", "aiohttp", "asyncio", "urllib3", "blivedm"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _configured = True
    return log_file


def get_logger(name: str) -> logging.Logger:
    """取一个具名 logger。

    名字会自动加上 ``danmaku_kb.`` 前缀（如果还没有），
    这样 ``__name__`` 与手写名字都能用。
    """
    if not name.startswith("danmaku_kb"):
        name = f"danmaku_kb.{name}"
    return logging.getLogger(name)


def get_raw_danmaku_logger() -> logging.Logger:
    """取弹幕原文 logger。"""
    return logging.getLogger("danmaku_kb.danmaku.raw")


__all__ = [
    "get_log_dir",
    "get_logger",
    "get_raw_danmaku_logger",
    "setup_logging",
]
