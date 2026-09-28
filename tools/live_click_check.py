#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收"点击触发"：把 QQ 置前 → 鼠标移到某条消息上 → 注入一次左键单击（打开面板）
→ 再点一次（收起）。仅用于验收，产品代码只读按键状态、不注入。"""

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
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004


def click() -> None:
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def main() -> int:
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = app_config.load_config()
    reader = MessageReader(cfg)
    messages, snapshot = [], {}
    for _ in range(10):
        messages, snapshot = reader.read()
        if messages:
            break
        time.sleep(1.5)
    target = None
    for msg in messages:
        if msg.side == "left" and not msg.is_image and (not want or want in msg.text):
            target = msg
            break
    if target is None:
        print("当前可见:", [m.text[:14] for m in messages])
        return 1
    hwnd = int((snapshot.get("window") or {})["hwnd"])
    before_cursor = w32.get_cursor_pos()
    cx = (target.bbox[0] + target.bbox[2]) // 2
    cy = (target.bbox[1] + target.bbox[3]) // 2
    print(f"目标：{target.text[:16]!r} bbox={target.bbox} → 点 ({cx},{cy})")
    w32.bring_window_to_front(hwnd)
    time.sleep(0.4)
    user32.SetCursorPos(int(cx), int(cy))
    time.sleep(0.3)
    click()
    print("第 1 次点击（应开始分析）")
    time.sleep(5)
    click()
    print("第 2 次点击（应收起浮窗）")
    time.sleep(1)
    user32.SetCursorPos(int(before_cursor[0]), int(before_cursor[1]))
    print("已恢复鼠标", w32.get_cursor_pos())
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
