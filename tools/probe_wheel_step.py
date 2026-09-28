#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：量"一格滚轮 = 消息移动多少像素"（用于滚轮预测跟随的估算值）。
每格之间等 0.6s，留出 Chromium 更新无障碍矩形的时间，避免把多格并成一格。"""

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
    w32.bring_window_to_front(hwnd)
    time.sleep(0.4)
    user32.SetCursorPos(int((target.bbox[0] + target.bbox[2]) // 2),
                        int((target.bbox[1] + target.bbox[3]) // 2))
    time.sleep(0.4)
    key = target.msg_key
    print(f"目标 {target.text[:14]!r} 起始 y={target.bbox[1]}")
    prev = reader.refresh_one(key)
    print("逐格注入滚轮（每格后等 0.6s 再读坐标）：")
    deltas = []
    for index in range(6):
        user32.mouse_event(WHEEL, 0, 0, -120, 0)      # 向上滚一格
        time.sleep(0.6)
        rect = reader.refresh_one(key)
        if rect is None or prev is None:
            print(f"  第{index + 1}格：坐标读不到（元素被回收）")
            prev = rect or prev
            continue
        delta = rect[1] - prev[1]
        deltas.append(delta)
        print(f"  第{index + 1}格：y {prev[1]} → {rect[1]}（Δ={delta:+d}px）")
        prev = rect
    if deltas:
        print(f"\n每格位移：{deltas}｜中位 {sorted(deltas)[len(deltas) // 2]}px")
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
