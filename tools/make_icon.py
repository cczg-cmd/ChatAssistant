#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成应用图标（简约风）：渲染多尺寸 PNG → 组装成 .ico。

用法：cache\\py311\\python.exe tools\\make_icon.py
产物：assets/icon.png（512，预览用）、assets/icon.ico（含 16/24/32/48/64/128/256）

设计（只用几何图形，不用位图素材，缩到 16px 也不糊）：
  · **背景全透明 + 线条镂空**（用户口径："图标改成紫粉色线条透明镂空设计比较好看"）：
    气泡与三条线都**只描边、不填充**（内部是镂空的，透出桌面/任务栏底色）；
  · 描边用**粉→紫渐变**（#FF9ECB → #A98BFF，取自 galgame 主题的 strip）；
  · 右下角一颗同渐变的实心圆点（"分析结果"）——纯镂空的话 16px 下会看不见，
    留一个实心焦点，小尺寸也认得出来。
ICO 用 **PNG 内嵌**写法（Vista+ 支持），不需要 Pillow。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "assets"

from PySide6.QtCore import QPointF, QRectF, Qt              # noqa: E402
from PySide6.QtGui import (QBrush, QColor, QImage, QLinearGradient,  # noqa: E402
                           QPainter, QPainterPath, QPen)

PINK = "#FF9ECB"
PURPLE = "#8B6BD9"
DARK = "#2A1B3D"          # 描边/线条（galgame 主题的暗紫）
WHITE = "#FFFFFF"


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    unit = size / 512.0                      # 以 512 为设计基准等比缩放
    gradient = QLinearGradient(0, 0, size, size)
    gradient.setColorAt(0.0, QColor(PINK))
    gradient.setColorAt(1.0, QColor(PURPLE))

    # 对话气泡（圆角矩形 + 左下小尾巴）：**只描边、不填充**
    bubble = QPainterPath()
    bubble_rect = QRectF(58 * unit, 104 * unit, 372 * unit, 236 * unit)
    bubble.addRoundedRect(bubble_rect, 62 * unit, 62 * unit)
    tail = QPainterPath()
    tail.moveTo(QPointF(142 * unit, 330 * unit))
    tail.lineTo(QPointF(142 * unit, 414 * unit))
    tail.lineTo(QPointF(232 * unit, 338 * unit))
    tail.closeSubpath()
    bubble = bubble.united(tail)
    painter.setBrush(Qt.NoBrush)
    painter.setPen(QPen(QBrush(gradient), max(1.0, 30 * unit),
                        Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.drawPath(bubble)

    # 气泡里的三条短线（"上下文/消息"）
    painter.setPen(QPen(QBrush(gradient), max(1.0, 26 * unit),
                        Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    for index, width in enumerate((196, 148, 104)):
        y = (186 + index * 54) * unit
        painter.drawLine(QPointF(118 * unit, y), QPointF((118 + width) * unit, y))

    # 右下角实心渐变圆点（"分析结果"）：小尺寸下唯一的实心焦点
    dot_center = QPointF(372 * unit, 350 * unit)
    dot_r = 50 * unit
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(gradient))
    painter.drawEllipse(dot_center, dot_r, dot_r)
    painter.setBrush(Qt.NoBrush)
    painter.setPen(Qt.NoPen)

    painter.end()
    return image


def write_ico(images, path: Path) -> None:
    """把若干 PNG 直接塞进 ICO 容器（不需要 Pillow）。"""
    entries = []
    for image in images:
        size = image.width()
        png_path = OUT_DIR / f"_tmp_{size}.png"
        image.save(str(png_path), "PNG")
        entries.append((size, png_path.read_bytes()))
        png_path.unlink(missing_ok=True)
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    directory = b""
    payload = b""
    for size, data in entries:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        payload += data
    path.write_bytes(header + directory + payload)


def main() -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)      # noqa: F841
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sizes = (16, 24, 32, 48, 64, 128, 256)
    images = [render(size) for size in sizes]
    preview = render(512)
    preview.save(str(OUT_DIR / "icon.png"), "PNG")
    write_ico(images, OUT_DIR / "icon.ico")
    print(f"已生成 {OUT_DIR / 'icon.png'}（512 预览）与 {OUT_DIR / 'icon.ico'}"
          f"（{len(sizes)} 种尺寸：{', '.join(str(s) for s in sizes)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
