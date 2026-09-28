#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切会话延迟对照实验（脚本自己注入点击，时序完全一致，无需人工操作）。

用法：python tmp/exp_switch_auto.py           # 默认点 3 个不同会话
输出：每次"注入点击 → 窗口标题变化"的耗时；用于对比"我们的程序运行中 vs 完全关闭"。
只点左侧会话列表的固定坐标，不点任何按钮；结束后把鼠标移回原位。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()

user32 = ctypes.WinDLL("user32", use_last_error=True)


def cursor() -> tuple:
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    point = POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return (point.x, point.y)


def title_of(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 2)
    user32.GetWindowTextW(hwnd, buf, length + 2)
    return buf.value


def click_at(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.12)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def main() -> int:
    window = w32.find_qq_chat_window("Chrome_WidgetWin_1", "qq.exe")
    if window is None:
        print("没找到 QQ 聊天窗口")
        return 1
    hwnd = int(window.hwnd)
    x0, y0, x1, y1 = window.rect
    before = cursor()
    w32.bring_window_to_front(hwnd)
    time.sleep(0.5)
    # 会话列表在窗口左侧：取 3 个不同行高的点（都落在列表里，不碰任何按钮）
    # 三行都取"实测能触发切换"的会话行（y0+240 / 360 / 480 = 窗口内 240/360/480 行高附近）
    targets = [(x0 + 130, y0 + 242), (x0 + 130, y0 + 362), (x0 + 130, y0 + 482)]
    print(f"QQ 窗口={window.rect}｜起始标题={title_of(hwnd)!r}")
    results = []
    for index, (cx, cy) in enumerate(targets, start=1):
        last = title_of(hwnd)
        click_at(cx, cy)
        t0 = time.perf_counter()
        changed, new_title = None, last
        while time.perf_counter() - t0 < 5.0:
            current = title_of(hwnd)
            if current != last:
                changed = (time.perf_counter() - t0) * 1000
                new_title = current
                break
            time.sleep(0.008)
        print(f"  第{index}次 点击({cx},{cy})："
              + (f"{changed:.0f} ms 后标题 → {new_title!r}" if changed else "5s 内标题未变"))
        results.append(changed)
        time.sleep(2.5)                     # 让 QQ 切完再点下一个
    user32.SetCursorPos(int(before[0]), int(before[1]))
    valid = [r for r in results if r]
    if valid:
        print(f"\n有效样本 {len(valid)}/3｜耗时 {[round(v) for v in valid]} ms")
    print("（对照：把我们的程序完全关闭后，用同样命令再跑一次，对比这三组数字）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
