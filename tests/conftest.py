"""pytest 共享夹具。

设计原则：**全部离线**。不联网、不开界面、不真的发按键。
参考项目的测试也是离线的，但它漏了"引擎流水线"这一层——本套件补上。
"""

from __future__ import annotations

import itertools
import shutil
import sys
from pathlib import Path

import pytest

# 让测试无需安装即可 import screenkey
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 自管临时目录的根。刻意**不**用 `.pytest_tmp`：那个名字下已经躺着 pytest
# 早期版本建出的 0700 目录，本环境删不掉（详见下面 tmp_path 的说明）。
TEST_TMP_ROOT = PROJECT_ROOT / ".test_tmp"
_tmp_counter = itertools.count()

# 采集时必须跳过的目录。
#
# 受限沙箱里 pytest 早期版本留下了一批**用 0700 建的、删也删不掉的**目录
# （`pytest-cache-files-*` 与调试用的 `_probe*`）。pytest 默认的
# ``norecursedirs`` 只有 ``.*`` / ``*.egg`` / ``build`` / ``dist`` 之类，
# **不匹配** `pytest-cache-files-*` 与 `_probe*`，于是裸跑 `pytest` 时
# 它会对这些目录做 ``iterdir()`` 并当场抛：

#     PermissionError: [WinError 5] 拒绝访问。: '...\\pytest-cache-files-oy59pck2'
#     !!!!!!!!!!!!!!!!!! Interrupted: 10 errors during collection !!!!!!!!!!!!!!!!!!

# 这纯属环境残留、与被测代码无关。这里把默认值连同这些模式一起设进去，
# 让 ``pytest``（不带路径）也能跑得跟 ``pytest tests`` 一样干净。
_IGNORED_DIRS = (
    "*.egg",
    ".*",
    "_darcs",
    "build",
    "CVS",
    "dist",
    "node_modules",
    "venv",
    "{arch}",
    "pytest-cache-files-*",
    "_probe*",
)


def pytest_configure(config: pytest.Config) -> None:
    """把环境残留目录排除出采集范围（原因见 ``_IGNORED_DIRS`` 上方注释）。

    ``norecursedirs`` 是 **ini 选项**（``_pytest/main.py:468``
    ``config.getini("norecursedirs")``），不在 ``config.option`` 上——
    直接给 ``config.option.norecursedirs`` 赋值会抛
    ``AttributeError: 'Namespace' object has no attribute 'norecursedirs'``。
    正确做法是用 ``addinivalue_line`` 往 ini 值里追加。
    """

    for pattern in _IGNORED_DIRS:
        config.addinivalue_line("norecursedirs", pattern)


def _safe_rmtree(path: Path) -> None:
    """尽力删除；删不掉**不算失败**。

    受限沙箱里某些目录连安全描述符都读不到，删除会抛 WinError 5。
    临时目录残留不影响测试结论，所以这里吞掉 OSError 而不是让 fixture 报错。
    """

    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """整个会话结束时清掉自管临时目录。

    内置的 session 级收尾是 ``_pytest/tmpdir.py:337`` 的
    ``cleanup_dead_symlinks(basetemp)``，它会去 ``iterdir()`` 那个 0700 目录，
    在本环境直接抛。既然 ``tmp_path`` / ``tmp_path_factory`` 都换成自管的了，
    收尾也要自己来。

    只管 ``tmp_path`` 与 ``tmp_path_factory`` 真正用到的两个子目录，
    **不动** ``.test_tmp/`` 下其他内容（例如手工脚本留下的目录）——
    避免误删不属于本次会话的东西。
    """

    for name in ("session",):
        _safe_rmtree(TEST_TMP_ROOT / name)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    """最后再扫一遍每个测试留下的目录（正常情况下 fixture 已经删过了）。"""

    if not TEST_TMP_ROOT.is_dir():
        return
    try:
        for child in TEST_TMP_ROOT.iterdir():
            if child.is_dir() and child.name != "session":
                _safe_rmtree(child)
    except OSError:
        # 目录不可枚举就放弃清理；不影响测试结论
        pass


@pytest.fixture
def tmp_path(request):
    """替代 pytest 内置的 ``tmp_path``：自管目录，用**默认权限**创建。

    为什么必须替代内置实现：``_pytest/tmpdir.py`` 一律用 ``mode=0o700`` 建目录
    （``:139`` ``p.mkdir(mode=0o700)``，另见 ``:158``/``:168``/``:172``/``:218``），
    而本工作区的受限令牌**无法访问 0700 目录**——实测 ``mkdir(mode=0o700)`` 之后
    list / write / rmdir 全部失败（``[WinError 5] 拒绝访问``、``[Errno 13] Permission denied``），
    而默认模式与 ``0o750`` / ``0o755`` / ``0o770`` / ``0o777`` 全部正常，
    顺序无关、重复 6 次结果一致。内置 ``tmp_path`` 因此在**夹具解析阶段**就崩：

        PermissionError: [WinError 5] 拒绝访问。:
        '...\\AppData\\Local\\Temp\\dsh-XXXX\\pytest-of-<user>'

    这不是被测代码的问题，而是"pytest 建的目录恰好是本环境唯一不可用的权限组合"。
    自管实现用不带 mode 的 ``Path.mkdir()``（得到默认权限），落在项目内的
    ``.test_tmp/`` 下，因此**既不碰系统临时目录，也不碰 0700 组合**。

    顺带一提：该目录位于项目内、已在 .gitignore 里被忽略。
    """

    name = "".join(c if c.isalnum() or c in "-_" else "_" for c in request.node.name)[:60]
    base = TEST_TMP_ROOT / f"{name}-{next(_tmp_counter)}"
    base.mkdir(parents=True, exist_ok=True)
    yield base
    _safe_rmtree(base)


@pytest.fixture(scope="session")
def tmp_path_factory():
    """内置 ``tmp_path_factory`` 的替代品，同样避开 0700。

    内置那个既会建 0700 目录，也会在 session 结束时调
    ``cleanup_dead_symlinks()`` 去 ``iterdir()``——在本环境同样被拒。
    """

    class _Factory:
        def __init__(self) -> None:
            self._root = TEST_TMP_ROOT / "session"
            self._root.mkdir(parents=True, exist_ok=True)

        def mktemp(self, basename: str = "tmp", numbered: bool = True) -> Path:
            suffix = f"-{next(_tmp_counter)}" if numbered else ""
            path = self._root / f"{basename}{suffix}"
            path.mkdir(parents=True, exist_ok=True)
            return path

        def getbasetemp(self) -> Path:
            return self._root

    return _Factory()


@pytest.fixture
def config_dir(tmp_path):
    """隔离的配置目录。"""

    return tmp_path / "cfg"


@pytest.fixture
def manager(config_dir):
    from screenkey.config.manager import ConfigManager

    return ConfigManager(config_dir=config_dir, auto_save=False)


@pytest.fixture
def engine():
    """dry_run 引擎：跑完整逻辑但不真的发按键。"""

    from screenkey.engine import ScreenKeyEngine

    eng = ScreenKeyEngine()
    eng.dry_run = True
    return eng


@pytest.fixture
def simulator():
    """模拟抓取器。"""

    from screenkey.danmaku.simulator import SimulatorFetcher

    return SimulatorFetcher(interval=0.01)


@pytest.fixture
def chat():
    """构造一条普通弹幕的工厂。"""

    from screenkey.danmaku.message import DanmakuMessage, MessageType

    def _make(content: str, nickname: str = "测试用户", user_id: int = 1, **kwargs):
        return DanmakuMessage(
            content=content,
            nickname=nickname,
            user_id=user_id,
            msg_type=kwargs.pop("msg_type", MessageType.DANMAKU),
            platform=kwargs.pop("platform", "simulator"),
            **kwargs,
        )

    return _make
