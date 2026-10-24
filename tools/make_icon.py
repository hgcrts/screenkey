# -*- coding: utf-8 -*-
"""生成幕键 ScreenKey 的应用图标（PNG 多尺寸 + 多尺寸 ICO）。

设计：深色圆角底 + 键帽 + 弹幕气泡（右下角带尾巴、三个点），
16px 下仍能辨认「键」与「气泡」两块主体。

用法：
    python tools/make_icon.py            # 写入 src/screenkey/assets/
    python tools/make_icon.py --outdir X # 写到其它目录（预览用）

只依赖 PyQt5（项目本来就依赖它），不引入 Pillow。
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import (
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
)

SIZES = (256, 128, 64, 48, 32, 24, 16)

BACKGROUND_TOP = QColor("#232B3D")
BACKGROUND_BOTTOM = QColor("#0D1119")
KEY_TOP = QColor("#F7F9FC")
KEY_BOTTOM = QColor("#B9C6DA")
KEY_EDGE = QColor("#8C9BB3")
ACCENT = QColor("#FF7C4D")


def _rounded(rect: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    return path


def draw_icon(size: int) -> QImage:
    """按 256 画布的比例绘制，最后缩放到目标尺寸。"""

    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

    # 在 256 坐标系里画，再整体缩放到目标尺寸
    painter.scale(size / 256.0, size / 256.0)

    # 背景：深色圆角方块 + 竖向渐变
    bg = _rounded(QRectF(8, 8, 240, 240), 54)
    grad = QLinearGradient(0, 8, 0, 248)
    grad.setColorAt(0.0, BACKGROUND_TOP)
    grad.setColorAt(1.0, BACKGROUND_BOTTOM)
    painter.setPen(Qt.NoPen)
    painter.setBrush(grad)
    painter.drawPath(bg)

    # 键帽：带底边厚度的方键，整体略偏左上，给气泡留位置
    key_rect = QRectF(46, 58, 132, 116)
    # 底边（背光阴影）
    shadow = _rounded(QRectF(key_rect.x(), key_rect.y() + 10, key_rect.width(), key_rect.height()), 20)
    painter.setBrush(QColor(0, 0, 0, 90))
    painter.drawPath(shadow)
    # 键面
    key_path = _rounded(key_rect, 20)
    key_grad = QLinearGradient(0, key_rect.y(), 0, key_rect.bottom())
    key_grad.setColorAt(0.0, KEY_TOP)
    key_grad.setColorAt(1.0, KEY_BOTTOM)
    painter.setBrush(key_grad)
    painter.setPen(QPen(KEY_EDGE, 3))
    painter.drawPath(key_path)

    # 键帽内部的小圆点（键位标记），位置居中偏下
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(140, 155, 179, 235))
    painter.drawEllipse(QRectF(96, 122, 32, 32))

    # 弹幕气泡：橙色椭圆 + 左下尾巴 + 三个白点
    bubble_rect = QRectF(138, 132, 96, 74)
    tail = QPainterPath()
    tail.moveTo(150, 196)
    tail.lineTo(138, 222)
    tail.lineTo(172, 202)
    tail.closeSubpath()

    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(0, 0, 0, 80))
    painter.drawPath(_rounded(QRectF(bubble_rect.x(), bubble_rect.y() + 8, bubble_rect.width(), bubble_rect.height()), 30))
    painter.setBrush(ACCENT)
    painter.drawPath(_rounded(bubble_rect, 30))
    painter.drawPath(tail)

    # 气泡里的三个点，模拟正在滚动的弹幕
    painter.setBrush(QColor("#FFF3EC"))
    dot_w = 11.0
    gap = 8.0
    total = dot_w * 3 + gap * 2
    start_x = bubble_rect.center().x() - total / 2
    center_y = bubble_rect.center().y()
    for index in range(3):
        painter.drawEllipse(
            QRectF(start_x + index * (dot_w + gap), center_y - dot_w / 2, dot_w, dot_w)
        )

    painter.end()
    return image


def write_png(path: Path, size: int) -> None:
    image = draw_icon(size)
    if not image.save(str(path), "PNG"):
        raise SystemExit(f"无法写入 {path}")


def write_ico(path: Path, sizes=SIZES) -> None:
    """手写 ICO：目录 + 内嵌 PNG 数据（Windows Vista 起支持 PNG 压缩条目）。"""

    payloads: list[tuple[int, bytes]] = []
    for size in sizes:
        image = draw_icon(size)
        from PyQt5.QtCore import QBuffer, QByteArray, QIODevice

        buffer = QBuffer(QByteArray())
        buffer.open(QIODevice.WriteOnly)
        image.save(buffer, "PNG")
        buffer.close()
        payloads.append((size, bytes(buffer.data())))

    header = struct.pack("<HHH", 0, 1, len(payloads))
    offset = 6 + 16 * len(payloads)
    entries = b""
    data = b""
    for size, payload in payloads:
        dim = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII",
            dim,          # width
            dim,          # height
            0,            # 调色板数量
            0,            # 保留
            1,            # 色彩平面
            32,           # 位深
            len(payload), # 数据长度
            offset,       # 数据偏移
        )
        data += payload
        offset += len(payload)

    path.write_bytes(header + entries + data)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="生成 ScreenKey 图标")
    parser.add_argument(
        "--outdir",
        default=str(Path(__file__).resolve().parents[1] / "src" / "screenkey" / "assets"),
        help="输出目录（默认 src/screenkey/assets）",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="额外在终端打印 32×16 的字符预览（无图形界面时用来核对形状）",
    )
    args = parser.parse_args(argv)

    # 离屏 QGuiApplication：QPainter/QImage.save 的 PNG 插件需要它，否则 Windows 上会崩
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication([])
    try:
        out = Path(args.outdir)
        out.mkdir(parents=True, exist_ok=True)
        write_png(out / "icon.png", 256)
        write_ico(out / "icon.ico")
        print(f"已生成：{out / 'icon.png'}（256×256）、{out / 'icon.ico'}（{len(SIZES)} 个尺寸）")
        if args.preview:
            print_ascii_preview(64)
        return 0
    finally:
        app.quit()


def print_ascii_preview(size: int) -> None:
    """把图标转成字符画，用来在没有看图环境时确认主体位置。"""

    image = draw_icon(size)
    ramp = " .:-=+*#%@"
    rows = 24
    columns = 48
    for row in range(rows):
        line = []
        for column in range(columns):
            pixel = image.pixelColor(
                min(size - 1, int((column + 0.5) * size / columns)),
                min(size - 1, int((row + 0.5) * size / rows)),
            )
            # 透明处理成空格；否则按亮度映射
            if pixel.alpha() < 40:
                line.append(" ")
                continue
            luminance = 0.299 * pixel.red() + 0.587 * pixel.green() + 0.114 * pixel.blue()
            line.append(ramp[min(len(ramp) - 1, int(luminance / 256.0 * len(ramp)))])
        print("".join(line))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
