#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断脚本：向上滚动几轮，逐轮打印
  ① 屏幕上实际顺序（按 y 坐标，即肉眼看到的从上到下）
  ② 我们的 row_index 顺序（UIA 行下标）
  ③ 池子按下标排序后的结果
用于定位"滚动几下顺序就乱"到底断在哪一环。
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

import config as app_config  # noqa: E402
from core.message_reader import MessageReader  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
WHEEL = 0x0800


def show(tag: str, messages, pool=None) -> None:
    print(f"\n--- {tag} ---")
    print("  屏幕顺序（按 y）:")
    for m in sorted(messages, key=lambda x: x.bbox[1]):
        kind = "[图]" if m.is_image else ("对方" if m.side == "left" else "我  ")
        print(f"    y={m.bbox[1]:<5} row={str(m.row_index):<5} {kind} {m.text[:22]}")
    print("  row_index 顺序:", [m.row_index for m in sorted(
        messages, key=lambda x: (x.row_index if x.row_index is not None else 10**9))])
    ys = [m.bbox[1] for m in sorted(messages, key=lambda x: x.bbox[1])]
    rows = [m.row_index for m in sorted(messages, key=lambda x: x.bbox[1])]
    plain = [r for r in rows if r is not None]
    print(f"  → 按 y 排的行下标是否单调: {plain == sorted(plain)}｜下标缺失条数: {rows.count(None)}")
    if pool is not None:
        print("  池子（按下标排序后）:", [(m.row_index, m.text[:12]) for m in pool[-12:]])


def main() -> int:
    cfg = app_config.load_config()
    reader = MessageReader(cfg)
    messages, snapshot = [], {}
    for _ in range(10):
        messages, snapshot = reader.read()
        if messages:
            break
        time.sleep(1.5)
    if not messages:
        print("读不到消息:", snapshot.get("status"))
        return 1
    hwnd = int((snapshot.get("window") or {})["hwnd"])
    w32.bring_window_to_front(hwnd)
    time.sleep(0.4)
    # 把鼠标放到聊天区中部，向上滚
    box = (snapshot.get("uia") or {}).get("list_box") or (0, 0, 800, 600)
    user32.SetCursorPos(int((box[0] + box[2]) / 2), int((box[1] + box[3]) / 2))
    time.sleep(0.3)
    show("滚动前", messages, reader._history)
    for round_index in range(1, 4):
        for _ in range(4):                       # 每轮向上滚 4 格
            user32.mouse_event(WHEEL, 0, 0, 120, 0)
            time.sleep(0.12)
        time.sleep(1.2)                          # 让 UIA 稳定 + 我们多读几轮
        for _ in range(3):
            messages, snapshot = reader.read()
            time.sleep(0.3)
        show(f"向上滚 {round_index * 4} 格后", messages, reader._history)
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
