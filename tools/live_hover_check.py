#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实机确认（受控、可恢复）：把 QQ 置前 + 鼠标移到指定消息上，等面板出来截屏，
然后**恢复鼠标位置与前台窗口**。只用于验收，产品代码不这么做。
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


def cursor_pos() -> tuple:
    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    point = POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return (point.x, point.y)


def main() -> int:
    want = sys.argv[1] if len(sys.argv) > 1 else "我现在打开游戏就会掉网"
    cfg = app_config.load_config()
    reader = MessageReader(cfg)
    messages, snapshot = [], {}
    for _ in range(10):
        messages, snapshot = reader.read()
        if messages:
            break
        time.sleep(1.5)
    target = next((m for m in messages if want in m.text and m.side == "left"), None)
    if target is None:
        print(f"没找到消息 {want!r}；当前可见：{[m.text for m in messages]}")
        return 1
    hwnd = int((snapshot.get("window") or {})["hwnd"])
    before_fore = w32.get_foreground_window()
    before_cursor = cursor_pos()
    cx = (target.bbox[0] + target.bbox[2]) // 2
    cy = (target.bbox[1] + target.bbox[3]) // 2
    print(f"目标：{target.text!r} bbox={target.bbox} → 鼠标放到 ({cx},{cy})；"
          f"原前台 hwnd={before_fore}，原鼠标 {before_cursor}")
    print("置前:", w32.bring_window_to_front(hwnd))
    time.sleep(0.4)
    user32.SetCursorPos(int(cx), int(cy))
    for wait_s in (2, 4, 6):
        time.sleep(2)
        print(f"  等待 {wait_s}s…")
    # 悬停状态下截屏（桌面复制，把我们的浮窗也拍进去）
    sys.path.insert(0, str(ROOT / "spikes"))
    import numpy as np  # noqa: E402
    import spike_wgc as wgc  # noqa: E402
    from PIL import Image  # noqa: E402
    out = ROOT / "tmp" / "live_hover_panel.png"
    dup = wgc.DxgiDuplicator()
    dup.open()
    try:
        shot, info = dup.grab(timeout_ms=1000)
    finally:
        dup.close()
    if shot is not None:
        rgb = shot[:, :, [2, 1, 0]] if shot.ndim == 3 else shot
        left, top, right, bottom = target.bbox
        x0 = max(0, left - 40)
        y0 = max(0, top - 260)
        x1 = min(rgb.shape[1], right + 700)
        y1 = min(rgb.shape[0], bottom + 700)
        crop = rgb[y0:y1, x0:x1]
        Image.fromarray(np.ascontiguousarray(crop)).save(out)
        print(f"已截屏 {out}（{crop.shape[1]}x{crop.shape[0]}）")
    # 恢复
    user32.SetCursorPos(int(before_cursor[0]), int(before_cursor[1]))
    if before_fore:
        user32.SetForegroundWindow(before_fore)
    print(f"已恢复鼠标 {cursor_pos()} 与前台 hwnd={w32.get_foreground_window()}")
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
