"""二维码 PNG 渲染。

设计取舍
--------
* 优先用 ``segno``（纯 Python、无 Qt 依赖、体积极小）。它是唯一被列入
  requirements 的二维码库。
* ``segno`` 缺失时 **不抛异常、不要求用户装东西**：回落到一个自绘的占位 PNG，
  上面画出定位角并附一行提示文字。这样扫码窗口在最坏情况下仍然能打开，
  用户至少能看到"缺 segno，请 pip install segno"的说明，而不是一个崩溃对话框。

参考项目把二维码渲染直接塞在登录对话框里，导致"没装 segno 时整个登录功能
不可用"，且 segno 的异常直接冒泡到 Qt 事件循环里被吞掉。
"""

from __future__ import annotations

import io
import struct
import zlib
from typing import Optional

from ..utils.logging_setup import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover - 取决于环境
    import segno

    SEGNO_AVAILABLE = True
except Exception:  # pragma: no cover
    segno = None  # type: ignore[assignment]
    SEGNO_AVAILABLE = False

__all__ = [
    "SEGNO_AVAILABLE",
    "make_qr_png",
    "make_qr_png_file",
    "make_placeholder_png",
]

# 占位图使用 5x7 点阵，够画提示文字。
_FONT: dict = {
    " ": ["00000"] * 7,
    "!": ["00100", "00100", "00100", "00100", "00100", "00000", "00100"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11111", "00010", "00100", "00010", "00001", "10001", "01110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    ":": ["00000", "01100", "01100", "00000", "01100", "01100", "00000"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "D": ["11100", "10010", "10001", "10001", "10001", "10010", "11100"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "J": ["00111", "00010", "00010", "00010", "00010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
}


def _png(width: int, height: int, rgb_rows: list) -> bytes:
    """把 ``rgb_rows``（每行 bytes，长度 = width*3）编码成 PNG。"""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + row for row in rgb_rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def make_placeholder_png(text: str = "SEGNO MISSING", scale: int = 6, border: int = 4) -> bytes:
    """自绘占位 PNG：定位角 + 大写点阵文字。"""

    scale = max(2, int(scale))
    text = (text or "").upper()
    # 每字符 6 列（5 点阵 + 1 间隔），7 行。
    char_w, char_h = 6, 7
    cols = max(len(text), 1) * char_w
    rows = char_h + 2
    modules = rows + 2 * border
    # 画布边长必须同时容得下**文字宽度**与定位角/边框。
    #
    # 历史缺陷：这里原来只写 ``max(modules, 29)``，即只按行数算，
    # 完全没考虑 ``cols``。于是 ``make_placeholder_png("INSTALL SEGNO")``
    # 只画出前 4 个字符，剩下的被下面那句 ``if 0 <= px < size`` 静默裁掉——
    # 而这段文字正是"缺 segno，请 pip install segno"的唯一提示，
    # 用户看到的是一个残缺的乱码方框，等于提示丢失。
    size = max(modules, cols + 2 * border, 29)  # 视觉上保持方正的"二维码感"
    grid = [[False] * size for _ in range(size)]

    # 三个定位角（7x7），让占位图一眼看出是二维码位置。
    for ox, oy in ((border, border), (size - border - 7, border), (border, size - border - 7)):
        for y in range(7):
            for x in range(7):
                edge = x in (0, 6) or y in (0, 6)
                core = 2 <= x <= 4 and 2 <= y <= 4
                if edge or core:
                    grid[oy + y][ox + x] = True

    start_x = max(border, (size - cols) // 2)
    start_y = max(border, (size - rows) // 2)
    for i, ch in enumerate(text):
        glyph = _FONT.get(ch, _FONT[" "])
        for gy, line in enumerate(glyph):
            for gx, bit in enumerate(line):
                if bit == "1":
                    px, py = start_x + i * char_w + gx, start_y + gy
                    if 0 <= px < size and 0 <= py < size:
                        grid[py][px] = True

    white = b"\xff\xff\xff"
    black = b"\x00\x00\x00"
    rgb_rows = []
    for row in grid:
        line = bytearray()
        for cell in row:
            # 横向也要放大 ``scale`` 倍：PNG 头声明的是 ``size * scale`` 宽，
            # 每行就必须有 ``size * scale * 3`` 字节。历史缺陷只做了**纵向**
            # 复制（下面的 for 循环），横向仍是 ``size`` 个像素，于是每行少
            # ``size*(scale-1)*3`` 字节 —— PNG 结构非法，libpng 报
            # "bad adaptive filter value"，``QImage.loadFromData`` 返回 False，
            # 界面只显示"二维码渲染失败"，那句"pip install segno"的提示彻底丢失。
            pixel = black if cell else white
            line += pixel * scale
        pixel_row = bytes(line)
        for _ in range(scale):
            rgb_rows.append(pixel_row)
    return _png(size * scale, size * scale, rgb_rows)


def make_qr_png(text: str, scale: int = 6, border: int = 4, error: str = "m") -> bytes:
    """把 ``text`` 渲染成二维码 PNG 字节；失败时返回占位图（绝不抛异常）。"""

    if not text:
        return make_placeholder_png("NO DATA", scale, border)
    if SEGNO_AVAILABLE:
        try:
            buf = io.BytesIO()
            segno.make(text, error=error).save(buf, kind="png", scale=scale, border=border)
            data = buf.getvalue()
            if data:
                return data
            logger.warning("segno 生成了空 PNG，回落到占位图")
        except Exception as exc:  # pragma: no cover - segno 内部异常
            logger.warning("segno 渲染二维码失败：%s；回落到占位图", exc)
    else:
        logger.warning("未安装 segno，二维码用占位图代替（pip install segno 可修复）")
    return make_placeholder_png("INSTALL SEGNO", scale, border)


def make_qr_png_file(text: str, path: str, scale: int = 6, border: int = 4) -> Optional[str]:
    """渲染并写入文件，返回路径；写失败返回 ``None``。"""

    data = make_qr_png(text, scale=scale, border=border)
    try:
        with open(path, "wb") as fh:
            fh.write(data)
        return path
    except OSError as exc:
        logger.warning("写入二维码文件失败 %s：%s", path, exc)
        return None
