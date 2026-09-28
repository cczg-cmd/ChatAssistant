#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性测量脚本（只读）：
  1) 屏幕/QQ 窗口物理几何 → 判断"同排空白带"与"窗口外侧"各有多少可用宽度；
  2) 用真实 Qt 字体量出"情绪 · 意图"标题在各字号下需要的像素宽度。
结论用于选定 panel_max_width 与标题最小字号。不改任何产品代码。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import config  # noqa: E402  触发工作区隔离
from PySide6.QtGui import QFont, QFontMetrics  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from utils import win32_api as w32  # noqa: E402

TITLES = [
    "平淡 · 询问明天下午是否有水课",     # 实测里最长的真实组合（11+2 字）
    "好奇 · 询问电脑是否清理过灰尘",
    "赞叹 · 夸赞赚二次元的钱",
    "不满 · 抱怨电脑掉网",
    "开心 · 夸赞",
]
SIZES = [11, 10, 9, 8]
PAD = 24          # 左右各 12px 内边距


def main() -> int:
    app = QApplication.instance() or QApplication([])   # noqa: F841

    screen = w32.get_virtual_screen_rect()
    print(f"虚拟屏幕（物理）={screen} → 宽 {screen[2] - screen[0]} 高 {screen[3] - screen[1]}")
    try:
        hwnd = w32.find_qq_window()
        rect = w32.get_window_rect(hwnd)
        scale = w32.get_window_scale(hwnd)
        print(f"QQ 窗口（物理）={rect}｜缩放={scale}")
        print(f"窗口右侧可用（物理）={screen[2] - rect[2]} → 逻辑 {int((screen[2] - rect[2]) / (scale or 1))}")
        print(f"窗口逻辑宽高={int((rect[2] - rect[0]) / (scale or 1))}x{int((rect[3] - rect[1]) / (scale or 1))}")
    except Exception as exc:
        print(f"读取 QQ 窗口失败：{exc}")

    print("\n实测空白带（来自日志）：同排右侧空白 210 物理 = 140 逻辑\n")
    print(f"{'标题':<26}" + "".join(f"{s}pt".rjust(9) for s in SIZES))
    for title in TITLES:
        row = f"{title:<26}"
        for size in SIZES:
            font = QFont("Microsoft YaHei UI", size)
            font.setBold(True)
            width = QFontMetrics(font).horizontalAdvance(title) + PAD
            row += f"{width:>9}"
        print(row)

    print("\n各字号下能放下的最大字符数（面板宽 140 逻辑，可用 116px）：")
    for size in SIZES:
        font = QFont("Microsoft YaHei UI", size)
        font.setBold(True)
        metrics = QFontMetrics(font)
        probe = "测试中文字符宽度样本一下"
        per_char = metrics.horizontalAdvance(probe) / len(probe)
        print(f"  {size}pt：单字 ≈ {per_char:.1f}px → 116px 可放 {116 / per_char:.1f} 字")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
