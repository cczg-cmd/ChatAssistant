#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""core/screenshot.py - 极简屏幕读取（GDI BitBlt），用于"滚动位移估计"。

安全边界（SPEC 2.2）：**只读屏幕像素**——
  - 不装任何输入钩子、不注入键鼠、不读进程内存、不改窗口属性；
  - 因此它不可能像 WH_MOUSE_LL 那样让系统鼠标输入被挂起（2026-09-23 的事故教训）。

用途：QQ 的聊天内容在滚动时不会及时更新 UIA 矩形，但**屏幕像素每帧都在变**；
把聊天区左侧一条窄带抓下来做帧间互相关，就能得到内容真实位移（含滚轮、拖滚动条、
新消息插入等所有原因），从而做到实时跟随。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
from typing import Optional

import numpy as np

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def grab_gray(left: int, top: int, right: int, bottom: int) -> Optional[np.ndarray]:
    """抓屏幕一块矩形，返回灰度二维数组（uint8，行×列）；失败返回 None。

    实测成本：220×700 的窄带约 1-2ms（GDI BitBlt + GetDIBits），可 25-40Hz 调用。
    """
    width, height = int(right - left), int(bottom - top)
    if width <= 0 or height <= 0:
        return None
    screen = user32.GetDC(0)
    if not screen:
        return None
    mem = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    old = gdi32.SelectObject(mem, bitmap)
    try:
        if not gdi32.BitBlt(mem, 0, 0, width, height, screen, int(left), int(top), SRCCOPY):
            return None
        info = BITMAPINFOHEADER()
        info.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.biWidth = width
        info.biHeight = -height                 # 负 = 自上而下
        info.biPlanes = 1
        info.biBitCount = 32
        info.biCompression = BI_RGB
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(mem, bitmap, 0, height, buffer, ctypes.byref(info),
                               DIB_RGB_COLORS):
            return None
        pixels = np.frombuffer(buffer, dtype=np.uint8).reshape(height, width, 4)
        # BGRA → 灰度（整数近似：0.114B + 0.587G + 0.299R）
        gray = (pixels[:, :, 0].astype(np.uint16) * 114
                + pixels[:, :, 1].astype(np.uint16) * 587
                + pixels[:, :, 2].astype(np.uint16) * 299) // 1000
        return gray.astype(np.uint8)
    finally:
        gdi32.SelectObject(mem, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, screen)


def vertical_profile(strip: np.ndarray, columns: int = 0) -> np.ndarray:
    """把灰度窄带压成一维纵向投影（每行平均亮度），用于互相关求竖直位移。"""
    if strip is None or strip.size == 0:
        return np.zeros(0, dtype=np.float32)
    if columns and strip.shape[1] > columns:      # 只取靠左的若干列（更稳、更快）
        strip = strip[:, :columns]
    return strip.mean(axis=1).astype(np.float32)


def best_shift(prev: np.ndarray, cur: np.ndarray, max_shift: int = 400,
               step: int = 2) -> tuple:
    """在 ±max_shift 内找使 prev[y] ≈ cur[y+dy] 的整数位移 dy（+ 表示内容下移）。

    返回 (dy, 残差)。残差是匹配后的平均绝对差；越小越可信。
    """
    n = int(min(len(prev), len(cur)))
    if n < 40:
        return (0, 1e9)
    prev, cur = prev[:n], cur[:n]
    # 只搜"重叠≥65%"的位移：重叠太小会拿两段平坦背景比出 0 残差 → 假位移（踩过）
    limit = min(int(max_shift), max(0, int(n * 0.35)))
    base_score = float(np.abs(prev - cur).mean())    # dy=0（不动）的残差
    best_dy, best_score = 0, None
    for dy in range(-limit, limit + 1, max(1, step)):
        if dy >= 0:
            # 约定：dy>0 表示"内容在屏幕上往下移了 dy 像素"（消息 y 应 +dy）
            a, b = prev[:n - dy], cur[dy:]
        else:
            a, b = prev[-dy:], cur[:n + dy]
        if len(a) != len(b) or len(a) < n * 0.35:
            continue
        score = float(np.abs(a - b).mean())
        if best_score is None or score < best_score:
            best_dy, best_score = dy, score
    if best_score is None:
        return (0, 1e9)
    # 最优匹配必须"明显优于不动"，否则宁可报 0：聊天背景大面积纯色时这能挡住瞎猜
    if best_dy != 0 and best_score > base_score * 0.85:
        return (0, base_score)
    return (best_dy, best_score)
