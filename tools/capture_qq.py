#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/capture_qq.py - 抓一张 QQ 窗口截图（含我们自己的悬浮窗），用于人工/视觉验收。"""
from __future__ import annotations

import sys
from pathlib import Path

WORKSPACE = Path(r"D:\QQChatAssistant")
sys.path.insert(0, str(WORKSPACE / "spikes"))
import spike_wgc as wgc  # noqa: E402

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else WORKSPACE / "tmp" / "qq_shot.png"
    wgc.set_dpi_awareness()
    target = None
    for win in wgc.find_qq_windows():
        if target is None or win.rect[2] - win.rect[0] > target.rect[2] - target.rect[0]:
            target = win
    if target is None:
        print("没找到 QQ 窗口")
        return 1
    print("QQ 窗口:", target.hwnd, target.title, target.cls, target.rect)
    # 用桌面复制抓全屏，才能把"我们自己的悬浮窗"一起拍进去
    dup = wgc.DxgiDuplicator()
    dup.open()
    try:
        shot, info = dup.grab(timeout_ms=1000)
    finally:
        dup.close()
    if shot is None:
        print("桌面复制取帧失败:", info)
        return 1
    rgb = shot[:, :, [2, 1, 0]] if shot.ndim == 3 else shot
    left, top, right, bottom = target.rect
    left, top = max(0, left), max(0, top)
    right = min(rgb.shape[1], right)
    bottom = min(rgb.shape[0], bottom)
    crop = rgb[top:bottom, left:right]
    Image.fromarray(np.ascontiguousarray(crop)).save(out)
    print("已保存:", out, "尺寸:", crop.shape[1], "x", crop.shape[0], "| 帧信息:", str(info)[:120])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
