#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/probe_uia6.py - 优化写法对比：哪种 UIA 读法最省时间（决定后台轮询预算）。

A) 全树 descendants()（pywinauto 缓存）
B) 先按控件类型+标题直接定位「消息列表」，再只对该子树取 descendants()
C) 在 B 的基础上只对 Text/Group 节点取 rectangle()
各自跑 5 轮，取平均；同时输出读到的消息条数与文本。
"""
from __future__ import annotations

import ctypes
import json
import sys
import time
from pathlib import Path

WORKSPACE = Path(r"D:\QQChatAssistant")
sys.path.insert(0, str(WORKSPACE / "spikes"))
import spike_wgc as wgc  # noqa: E402

OUT = WORKSPACE / "tmp" / "probe_uia6.json"
user32 = ctypes.WinDLL("user32", use_last_error=True)
ROUNDS = 5


def pick_chat_window():
    best = None
    for hwnd, pid in wgc.enum_top_level_windows():
        try:
            if wgc.get_class_name(hwnd) != "Chrome_WidgetWin_1":
                continue
            exe = wgc.get_process_image_path(pid).lower()
        except Exception:
            continue
        if "qq.exe" not in exe or user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
            continue
        rect = wgc.get_window_rect(hwnd)
        area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
        if best is None or area > best["area"]:
            best = {"hwnd": hwnd, "rect": rect, "area": area}
    return best


def read_a(wrapper) -> list[str]:
    nodes = wrapper.descendants()
    return [n.element_info.name for n in nodes if (n.element_info.name or "").strip()]


def read_b(wrapper) -> list[str]:
    boxes = wrapper.descendants(control_type="Window", title="消息列表")
    if not boxes:
        return []
    box = boxes[0]
    nodes = box.descendants()
    out = []
    for n in nodes:
        info = n.element_info
        if info.control_type == "Text" and (info.name or "").strip():
            rect = n.rectangle()
            out.append(f"{(info.name or '').strip()}|{rect.left},{rect.top},{rect.width()}x{rect.height()}")
    return out


def main() -> int:
    wgc.set_dpi_awareness()
    win = pick_chat_window()
    if not win:
        print("没找到可见的 QQ 聊天窗口")
        return 1
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    wrapper = UIAWrapper(UIAElementInfo(win["hwnd"]))
    read_b(wrapper)  # 预热（触发 Chromium 构建无障碍树）

    stats = {}
    for name, fn in (("A_full_descendants", read_a), ("B_locate_list_then_read", read_b)):
        times, sample = [], []
        for _ in range(ROUNDS):
            t0 = time.perf_counter()
            sample = fn(wrapper)
            times.append((time.perf_counter() - t0) * 1000)
        stats[name] = {"avg_ms": round(sum(times) / len(times), 1),
                       "min_ms": round(min(times), 1), "max_ms": round(max(times), 1),
                       "items": len(sample)}

    # C: 事件/条件查询的另一种写法——直接找所有 Text（control_type 过滤，跨进程一次条件查询）
    times, sample_c = [], []
    for _ in range(ROUNDS):
        t0 = time.perf_counter()
        texts = wrapper.descendants(control_type="Text")
        rows = []
        for n in texts:
            nm = (n.element_info.name or "").strip()
            if not nm:
                continue
            r = n.rectangle()
            rows.append(f"{nm}|{r.left},{r.top},{r.width()}x{r.height()}")
        sample_c = rows
        times.append((time.perf_counter() - t0) * 1000)
    stats["C_filtered_text_all"] = {"avg_ms": round(sum(times) / len(times), 1),
                                    "min_ms": round(min(times), 1), "max_ms": round(max(times), 1),
                                    "items": len(sample_c)}

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print("--- B 读到的条目（前 12） ---")
    for row in sample:
        print("   ", row[:70])
    print("--- C 读到的条目（前 12） ---")
    for row in sample_c[:12]:
        print("   ", row[:70])
    OUT.write_text(json.dumps({"stats": stats, "sample_b": sample, "sample_c": sample_c},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
