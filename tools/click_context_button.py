#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收"上下文历史"入口：从日志里取状态灯左侧小图标的物理位置 → 注入一次左键点击。

为什么从日志取位置：那个小图标是我们自己的 Qt 窗口（不是 UIA 控件），
主程序每次定位都会打一行 `上下文历史图标位置（物理）=[x0,y0,x1,y1]`，直接用即可。
点击本身由 user32.mouse_event 注入（仅验收脚本；产品代码只读按键状态、不注入）。
"""

from __future__ import annotations

import ctypes
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()

import config as app_config  # noqa: E402
from core.message_reader import MessageReader  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
PATTERN = re.compile(r"上下文历史图标位置（物理）=\[(\d+), (\d+), (\d+), (\d+)\]")


def click() -> None:
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def last_button_rect(log_path: Path):
    rect = None
    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = PATTERN.search(line)
        if match:
            rect = [int(v) for v in match.groups()]
    return rect


def main() -> int:
    log_name = sys.argv[1] if len(sys.argv) > 1 else "logs/stdout_runA4.log"
    rect = last_button_rect(ROOT / log_name)
    if rect is None:
        print("日志里没有图标位置")
        return 1
    cfg = app_config.load_config()
    reader = MessageReader(cfg)
    messages, snapshot = [], {}
    for _ in range(6):
        messages, snapshot = reader.read()
        if messages:
            break
        time.sleep(1.0)
    reader.close()
    hwnd = (snapshot.get("window") or {}).get("hwnd")
    if hwnd:
        w32.bring_window_to_front(int(hwnd))
        time.sleep(0.5)
    before = w32.get_cursor_pos()
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    print(f"图标物理矩形={rect} → 点 ({cx},{cy})；同屏可见消息 {len(messages)} 条")
    user32.SetCursorPos(int(cx), int(cy))
    time.sleep(0.4)
    click()
    print("已点击（应打开上下文历史面板）")
    time.sleep(0.8)
    user32.SetCursorPos(int(before[0]), int(before[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
