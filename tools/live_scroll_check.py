#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收"滚动跟随"：点击一条消息打开面板 → 在聊天区注入滚轮 → 截屏两次对比
（面板应跟着目标消息走，而不是"顿一下传送"）。仅用于验收。"""

from __future__ import annotations

import ctypes
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
WHEEL = 0x0800


def click() -> None:
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(0x0004, 0, 0, 0, 0)


def wheel(notches: int) -> None:
    for _ in range(abs(notches)):
        user32.mouse_event(WHEEL, 0, 0, -120 if notches < 0 else 120, 0)
        time.sleep(0.08)


def main() -> int:
    cfg = app_config.load_config()
    reader = MessageReader(cfg)
    messages, snapshot = [], {}
    for _ in range(10):
        messages, snapshot = reader.read()
        if messages:
            break
        time.sleep(1.5)
    target = next((m for m in messages if m.side == "left" and not m.is_image), None)
    if target is None:
        print("当前可见:", [m.text[:12] for m in messages])
        return 1
    hwnd = int((snapshot.get("window") or {})["hwnd"])
    before = w32.get_cursor_pos()
    cx = (target.bbox[0] + target.bbox[2]) // 2
    cy = (target.bbox[1] + target.bbox[3]) // 2
    print(f"目标 {target.text[:14]!r} → 点 ({cx},{cy})")
    w32.bring_window_to_front(hwnd)
    time.sleep(0.4)
    user32.SetCursorPos(int(cx), int(cy))
    time.sleep(0.3)
    click()
    time.sleep(3.5)
    print("已打开面板；开始注入滚轮 3 格（向上滚）")
    wheel(3)
    time.sleep(1.2)
    user32.SetCursorPos(int(before[0]), int(before[1]))
    print("已恢复鼠标")
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
