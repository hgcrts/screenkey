# -*- coding: utf-8 -*-
r"""打包辅助：让 PyInstaller 能在**含非 ASCII 字符**的项目路径下工作。

为什么需要这个
--------------

PyQt5 5.15.x 的 ``QLibraryInfo`` 无法处理安装路径里的非 ASCII 字符。
实测（Windows 10 + Python 3.12 + PyQt5 5.15.11）：

    PrefixPath    : 'E:/DSHwork/????V2/.../PyQt5/Qt5'          [MISSING]
    PluginsPath   : 'E:/DSHwork/????V2/.../PyQt5/Qt5/plugins'  [MISSING]

四个汉字被替换成了四个 ``?``，于是 ``os.path.isdir()`` 全部为 False。
PyInstaller 的 Qt hook 会去读 ``PluginsPath`` 收集插件，拿到这个坏路径后直接：

    Exception: Qt plugin directory 'E:/DSHwork/????V2/.../plugins' does not exist!

**这不是 PyInstaller 的缺陷，PyInstaller 只是受害者。** 注意 Qt 本身并不是
完全不会处理中文路径（``QFileInfo(r"...\\弹幕键盘V2\\src").exists()`` 为 True），
只有 ``QLibraryInfo`` 这一条计算路径是坏的。

判别因子只有一条：**路径里有没有非 ASCII 字符**。已排除的假设包括
codecs 插件缺失、``locale.setlocale``、``QTextCodec.setCodecForLocale``、
``QT_PLUGIN_PATH``/``PYTHONUTF8``/``PYTHONLEGACYWINDOWSFSENCODING`` 等环境变量——
六种组合全部无效。

解决思路
--------

在纯 ASCII 的位置建一个**目录联接（junction）**指向真实项目目录，
再从 junction 构建。junction 是文件系统层的重定向，进程看到的路径
自始至终是 ASCII 的，而文件实际落在真实目录里。

用法::

    # 打印应当作为构建根的路径；必要时先建好 junction
    python tools/build_helper.py --ensure-ascii

    # 只检查，不创建任何东西
    python tools/build_helper.py --check

    # 把构建根写进文件，供批处理 set /p 读取
    python tools/build_helper.py --out "%TEMP%\dmkb2_build_root.txt"

stdout **只输出一行**：构建根路径。所有说明与错误走 stderr，
退出码 0 表示成功、1 表示失败。

``build.bat`` 走的是 ``--out`` 那条路（写文件 + ``set /p``），
而**不是** ``for /f`` 捕获 stdout：``for /f`` 解析带引号的命令时会报
「文件名、目录名或卷标语法不正确」，实测踩过。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

#: junction 位置的候选顺序。第一个能建成功的就用它。
#: 都要求是**纯 ASCII** 且可写。
LINK_CANDIDATES = (
    "DANMAKU_KB_BUILD_LINK",  # 环境变量优先，方便用户自己指定
    "%TEMP%",
    "%USERPROFILE%",
    "%SystemDrive%\\",
)

LINK_NAME = "dmkb2_build_link"


def is_ascii_path(path: Path) -> bool:
    """路径里是否**全是** ASCII 字符。"""
    return all(ord(ch) < 128 for ch in str(path))


def describe_non_ascii(path: Path) -> str:
    """列出路径里所有非 ASCII 字符，方便用户定位是哪个目录名的问题。"""
    bad = sorted({ch for ch in str(path) if ord(ch) >= 128})
    return "".join(bad)


def _link_exists(link: Path) -> bool:
    """``link`` 这个名字是否已被占用（**含悬空的 junction**）。

    这里**不能用 ``Path.exists()``**：它会跟随重解析点，目标目录一旦被删掉，
    一个明明还在的 junction 会报 ``False``——于是调用方以为"没有链接"，
    直接去 ``mklink``，而系统报「当文件已存在时，无法创建该文件」，
    构建永久失败且原因极难看出。实测：

        link.exists()      -> False   （悬空）
        os.path.lexists()  -> True    （名字在）
        link.is_junction() -> True

    ``os.path.lexists()`` 只看名字在不在，不跟随链接，正是这里要的语义。
    """
    try:
        return os.path.lexists(str(link))
    except OSError:
        return False


def _link_points_at(link: Path, target: Path) -> bool:
    """判断 ``link`` 是否已经是指向 ``target`` 的链接。

    用 ``Path.resolve()`` 而不是 ``dir /al`` 解析：Windows 上 ``resolve()`` 会
    跟随重解析点，直接把 junction 解析成真实目录，比解析本地化的 ``dir``
    输出可靠得多（中文系统上 ``dir /al`` 的列名与格式都可能变，
    曾因此把目标读成 ``None``，每次构建都白删重建一次）。

    存在性判断走 ``_link_exists()``，这样悬空的 junction 也能被认出来
    （见那里的说明）。
    """
    if not _link_exists(link):
        return False
    try:
        return link.resolve() == target.resolve()
    except OSError:
        return False


def _remove_link(link: Path) -> bool:
    """删掉 junction 本身。

    **必须用 ``os.rmdir``**：它只移除重解析点，不会碰目标目录里的内容。
    用 ``shutil.rmtree`` 有跟随链接删掉真实项目文件的风险。
    ``os.rmdir`` 对**悬空**的 junction 同样有效（它不跟随链接）。
    """
    try:
        os.rmdir(str(link))
        return True
    except OSError as exc:
        print(f"  [警告] 无法删除已存在的链接 {link}：{exc}", file=sys.stderr)
        return False


def _make_link(link: Path, target: Path) -> bool:
    """用 ``mklink /J`` 建目录联接。

    junction 不需要管理员权限（符号链接才需要）。
    """
    try:
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="gbk",
            errors="replace",
            # 没有超时的话，万一 mklink 挂在某个离线网络路径上，构建脚本会
            # 永久卡住且没有任何输出；30 秒足够建一个本地 junction。
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        print("  [失败] mklink 超过 30 秒未返回，已放弃。", file=sys.stderr)
        return False
    except OSError as exc:
        print(f"  [失败] 无法执行 mklink：{exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(f"  [失败] mklink 返回 {proc.returncode}：{proc.stdout.strip()}", file=sys.stderr)
        return False
    return True


def _pick_link_path() -> Optional[Path]:
    """挑一个可写的纯 ASCII 位置来放 junction。"""
    for raw in LINK_CANDIDATES:
        if raw.startswith("DANMAKU_KB_BUILD_LINK"):
            base_str = os.environ.get(raw, "")
            if not base_str:
                continue
        else:
            base_str = os.path.expandvars(raw)
        if not base_str:
            continue
        base = Path(base_str)
        if not is_ascii_path(base):
            continue
        if not base.is_dir():
            continue
        candidate = base / LINK_NAME
        try:
            # 探测可写性：junction 本身要在这里创建
            probe = base / f".__dmkb_probe_{os.getpid()}"
            probe.mkdir()
            probe.rmdir()
        except OSError:
            continue
        return candidate
    return None


def ensure_ascii_build_root(project_root: Path, create: bool = True) -> Optional[Path]:
    """返回应当作为构建根的路径。

    路径本身是 ASCII 时直接返回 ``project_root``；否则建/复用 junction。
    失败返回 ``None``。
    """
    project_root = project_root.resolve()

    if is_ascii_path(project_root):
        print(f"[检查] 项目路径全是 ASCII，可以直接构建：{project_root}", file=sys.stderr)
        return project_root

    print("[检查] 项目路径含非 ASCII 字符：", file=sys.stderr)
    print(f"       {project_root}", file=sys.stderr)
    print(f"       非 ASCII 字符：{describe_non_ascii(project_root)}", file=sys.stderr)
    print(
        "[原因] PyQt5 5.15.x 的 QLibraryInfo 处理不了非 ASCII 安装路径，\n"
        "       会把它们变成 '?'，导致 PyInstaller 的 Qt hook 报\n"
        "       \"Qt plugin directory '.../????' does not exist!\"。\n"
        "       这不是 PyInstaller 的问题，换 PyInstaller 版本也没用。",
        file=sys.stderr,
    )

    if not create:
        print("[结果] 仅检查模式：需要 ASCII junction 才能打包。", file=sys.stderr)
        return None

    link = _pick_link_path()
    if link is None:
        print(
            "[失败] 找不到可写的纯 ASCII 位置来建 junction。\n"
            "       可以自己指定一个，例如：\n"
            "           set DANMAKU_KB_BUILD_LINK=D:\\builds\n"
            "       或者干脆把项目移到纯 ASCII 路径（推荐，最省事）。",
            file=sys.stderr,
        )
        return None

    print(f"[处理] 在 {link} 建立指向项目目录的 junction …", file=sys.stderr)

    if _link_exists(link):
        if _link_points_at(link, project_root):
            print("       已存在且指向正确，直接复用。", file=sys.stderr)
            return link
        print("       已存在但不指向本项目，先删除再重建。", file=sys.stderr)
        if not _remove_link(link):
            return None

    if not _make_link(link, project_root):
        print(
            "[失败] 无法创建 junction。\n"
            "       请手工建一个纯 ASCII 路径，例如：\n"
            f'           mklink /J "C:\\dmkb2" "{project_root}"\n'
            "       然后从那个目录运行 build.bat。",
            file=sys.stderr,
        )
        return None

    if not (link / "main.spec").exists():
        # junction 是刚刚建出来的，但看不到 main.spec 说明它没指向本项目
        # （或者指向了一个残缺目录）。留着这个错误的联接会让下次运行直接
        # "复用"它，所以这里回滚掉，让下次能重新挑一个位置。
        print(
            f"[失败] junction 建好了（{link}），但看不到 main.spec，构建会缺配置。",
            file=sys.stderr,
        )
        if _remove_link(link):
            print("       已回滚该 junction。", file=sys.stderr)
        else:
            print(f"       回滚失败，请手动删除：{link}", file=sys.stderr)
        return None

    print(f"       完成：{link}", file=sys.stderr)
    print(
        "[说明] 产物仍会出现在真实项目目录里（junction 是透明的重定向）：\n"
        f"       {project_root}\\dist\\弹幕键盘V2\\",
        file=sys.stderr,
    )
    return link


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_helper",
        description="为 PyInstaller 准备一个纯 ASCII 的构建根路径",
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="项目根目录，默认取本脚本的上一级目录",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--ensure-ascii",
        action="store_true",
        help="必要时创建 junction（默认行为）",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="只检查，不创建任何东西",
    )
    parser.add_argument("--verbose", action="store_true", help="多打印一些信息")
    parser.add_argument(
        "--out",
        default=None,
        help=(
            "把构建根路径写进这个文件（UTF-8，无 BOM）。"
            "batch 里用 set /p 读它，比 for /f 可靠——"
            "for /f 处理带引号的命令时会把引号解析坏，报"
            "『文件名、目录名或卷标语法不正确』。"
        ),
    )

    args = parser.parse_args(argv)

    if args.project_root:
        project_root = Path(args.project_root)
    else:
        project_root = Path(__file__).resolve().parent.parent

    root = ensure_ascii_build_root(project_root, create=not args.check)
    if root is None:
        return 1

    if args.out:
        # 用 utf-8-sig 之外的空编码：读写都靠 Python，无 BOM 最省事。
        # 注意路径里可能有非 ASCII（项目本身是 ASCII 时 BUILD_ROOT 就是
        # 项目路径，可能含中文），所以不能按 ascii 写。
        out_path = Path(args.out)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(str(root), encoding="utf-8")
        except OSError as exc:
            print(f"[失败] 无法写入 {out_path}：{exc}", file=sys.stderr)
            return 1
        if args.verbose:
            print(f"[输出] 构建根已写入 {out_path}", file=sys.stderr)

    # stdout 只留一行，方便调用方直接取
    print(str(root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
