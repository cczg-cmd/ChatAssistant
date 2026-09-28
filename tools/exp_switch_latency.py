#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切会话延迟测量（只读，不注入）：测"你点了左边会话列表 → QQ 标题变化"的耗时。

用法：python tmp/exp_switch_latency.py 25      # 跑 25 秒，期间请点 3 次不同会话
输出：每次点击的时刻、点击坐标、标题变化时刻、耗时（ms）。
对比"我们的程序运行中"与"完全关闭后"两组数字，就能判定卡顿是否由我们造成。
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
VK_LBUTTON = 0x01


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


def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0
    window = w32.find_qq_chat_window("Chrome_WidgetWin_1", "qq.exe")
    if window is None:
        print("没找到 QQ 聊天窗口")
        return 1
    hwnd = int(window.hwnd)
    print(f"QQ 窗口 hwnd={hwnd}｜当前标题={title_of(hwnd)!r}")
    print(f"请在这 {duration:.0f} 秒内：把鼠标移到左侧会话列表，**点 3 次不同会话**（每次间隔 3 秒以上）")
    trace = []
    last_title = title_of(hwnd)
    down = False
    clicks = []
    t_end = time.time() + duration
    while time.time() < t_end:
        now = time.time()
        pressed = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        if pressed and not down:
            pos = cursor()
            clicks.append((now, pos))
            print(f"  [{now - t_end + duration:6.2f}s] 点击 @ {pos}")
        down = pressed
        title = title_of(hwnd)
        if title != last_title:
            trace.append((now, last_title, title))
            print(f"  [{now - t_end + duration:6.2f}s] 标题变化：{last_title!r} → {title!r}")
            last_title = title
        time.sleep(0.01)

    print("\n===== 结果：点击 → 标题变化 的耗时 =====")
    if not clicks:
        print("这次没有捕捉到点击（是不是没点会话列表？）")
    for index, (ts, pos) in enumerate(clicks, start=1):
        after = [(t, a, b) for t, a, b in trace if t >= ts]
        if not after:
            print(f"  第{index}次点击 @{pos}：{duration:.0f}s 内未见标题变化")
            continue
        delta = (after[0][0] - ts) * 1000
        print(f"  第{index}次点击 @{pos}：标题在 {delta:.0f} ms 后变化"
              f" → {after[0][2]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
