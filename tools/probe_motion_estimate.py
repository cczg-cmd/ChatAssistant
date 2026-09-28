#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：验证"用 WGC/DXGI 帧间互相关估计滚动位移"的精度。

做法：抓聊天区左侧一条窄带（避开我们自己的浮窗）→ 纵向灰度投影 → 与上一帧做互相关
求最佳位移 dy；同时用 UIA 读取目标消息的真实位移作为基准，两者对比。
全程只用屏幕读取 + 滚轮注入（不装钩子、不注入按键）。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "spikes"))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()

import numpy as np  # noqa: E402
import spike_wgc as wgc  # noqa: E402

import config as app_config  # noqa: E402
from core.message_reader import MessageReader  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
WHEEL = 0x0800


def profile(frame, left, top, right, bottom):
    """取矩形区域的灰度纵向投影（每行的平均亮度）。"""
    crop = frame[top:bottom, left:right]
    if crop.ndim == 3:
        crop = crop.mean(axis=2)
    return crop.mean(axis=1).astype(np.float32)


def best_shift(prev, cur, max_shift=400):
    """在 ±max_shift 里找使 prev[y] ≈ cur[y+dy] 的 dy（整数像素）。"""
    n = min(len(prev), len(cur))
    prev, cur = prev[:n], cur[:n]
    best, best_score = 0, None
    for dy in range(-max_shift, max_shift + 1, 2):
        if dy >= 0:
            a, b = prev[dy:], cur[:n - dy]
        else:
            a, b = prev[:n + dy], cur[-dy:]
        if len(a) < n * 0.4:
            continue
        score = float(np.abs(a - b).mean())
        if best_score is None or score < best_score:
            best, best_score = dy, score
    return best, best_score


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
        print("没读到消息:", [m.text[:12] for m in messages])
        return 1
    list_box = (snapshot.get("uia") or {}).get("list_box")
    print("消息列表:", list_box)
    # 左侧窄带（消息气泡/头像那一列），避开右侧的分析浮窗
    left = int(list_box[0]) + 10
    right = left + 220
    top = int(list_box[1]) + 10
    bottom = int(list_box[3]) - 10
    hwnd = int((snapshot.get("window") or {})["hwnd"])
    w32.bring_window_to_front(hwnd)
    time.sleep(0.4)
    user32.SetCursorPos(int((target.bbox[0] + target.bbox[2]) // 2),
                        int((target.bbox[1] + target.bbox[3]) // 2))
    time.sleep(0.3)
    dup = wgc.DxgiDuplicator()
    dup.open()
    try:
        frame, _ = dup.grab(timeout_ms=1000)
        prev_prof = profile(frame, left, top, right, bottom)
        prev_rect = reader.refresh_one(target.msg_key)
        print(f"起始 消息y={None if prev_rect is None else prev_rect[1]}"
              f"｜画面统计 mean={prev_prof.mean():.1f} std={prev_prof.std():.1f}"
              f"｜帧尺寸={frame.shape}")
        for trial in range(1, 4):
            direction = -120 if trial % 2 == 1 else 120      # 交替方向，避免"已在底部"导致不动
            for _ in range(3):                       # 连续注入 3 格（模拟一次快滚）
                user32.mouse_event(WHEEL, 0, 0, direction, 0)
                time.sleep(0.05)
            time.sleep(0.6)                          # 等 UIA 稳定（我们不做实时跟随，只验证精度）
            frame, _ = dup.grab(timeout_ms=1000)
            cur_prof = profile(frame, left, top, right, bottom)
            cur_rect = reader.refresh_one(target.msg_key)
            dy_est, score = best_shift(prev_prof, cur_prof)
            # 消息下移 = 画面内容下移 = cur 比 prev 的 y 大 → 相关里应为 +dy 方向
            truth = None if (cur_rect is None or prev_rect is None) else cur_rect[1] - prev_rect[1]
            print(f"  第{trial}轮（注入 {direction:+d}）：互相关 dy={dy_est:+d}px"
                  f"（残差 {score:.2f}，画面 std={cur_prof.std():.1f}）｜UIA 真实 Δy="
                  f"{truth if truth is None else format(truth, '+d')}")
            prev_prof = cur_prof
            prev_rect = cur_rect or prev_rect
    finally:
        dup.close()
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
