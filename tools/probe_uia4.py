#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/probe_uia4.py - 深挖：QQ 消息列表里的每条消息，UIA 能不能同时给出「文本 + bbox」。

结果写 tmp/probe_uia4.json（UTF-8），终端只打摘要。判定要点：
  1) 消息文本是否能逐条拿到（含发送者/时间/正文）；
  2) 每条消息是否有非零且落在"消息列表"范围内的 bbox（决定能否做"同排右侧空白"贴附）；
  3) 一次遍历耗时（决定能否实时刷新）。
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

OUT = WORKSPACE / "tmp" / "probe_uia4.json"
user32 = ctypes.WinDLL("user32", use_last_error=True)


def pick_chat_window() -> dict | None:
    best = None
    for hwnd, pid in wgc.enum_top_level_windows():
        try:
            if wgc.get_class_name(hwnd) != "Chrome_WidgetWin_1":
                continue
            exe = wgc.get_process_image_path(pid).lower()
        except Exception:
            continue
        if "qq.exe" not in exe:
            continue
        if user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
            continue
        rect = wgc.get_window_rect(hwnd)
        area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
        if best is None or area > best["area"]:
            best = {"hwnd": hwnd, "pid": pid, "rect": rect, "area": area,
                    "title": wgc.get_window_text(hwnd)}
    return best


def walk(wrapper: object, max_nodes: int = 4000) -> list[dict]:
    rows: list[dict] = []
    stack = [(wrapper, 0)]
    while stack and len(rows) < max_nodes:
        node, depth = stack.pop()
        try:
            info = node.element_info
            rect = node.rectangle()
            rows.append({
                "depth": depth,
                "control_type": info.control_type or "",
                "name": (info.name or "").strip(),
                "automation_id": (info.automation_id or ""),
                "rect": [rect.left, rect.top, rect.right, rect.bottom],
                "w": rect.width(), "h": rect.height(),
            })
            children = node.children()
        except Exception:
            continue
        for child in reversed(children):
            stack.append((child, depth + 1))
    return rows


def main() -> int:
    dpi = wgc.set_dpi_awareness()
    win = pick_chat_window()
    if not win:
        print("没找到可见的 QQ 聊天窗口")
        return 1
    print(f"DPI={dpi} QQ 窗口 hwnd={win['hwnd']} rect={win['rect']} title={win['title'][:30]!r}")

    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    t0 = time.perf_counter()
    wrapper = UIAWrapper(UIAElementInfo(win["hwnd"]))
    rows = walk(wrapper)
    elapsed = (time.perf_counter() - t0) * 1000
    named = [r for r in rows if r["name"]]
    with_rect = [r for r in named if r["w"] > 0 and r["h"] > 0]

    print(f"节点={len(rows)} 有名字={len(named)} 有非零bbox={len(with_rect)} 耗时={elapsed:.0f}ms")

    # 找"消息列表"容器
    list_box = None
    for r in rows:
        if r["name"] in ("消息列表",) and r["w"] > 0:
            list_box = r
            break
    print("消息列表容器:", list_box["rect"] if list_box else "未找到")

    inside = []
    if list_box:
        x0, y0, x1, y1 = list_box["rect"]
        inside = [r for r in with_rect
                  if x0 <= r["rect"][0] and r["rect"][2] <= x1
                  and y0 <= r["rect"][1] and r["rect"][3] <= y1]
    inside.sort(key=lambda r: (r["rect"][1], r["rect"][0]))
    print(f"落在消息列表内、且有 bbox 的节点 {len(inside)} 个（前 25 个按 y 排序）:")
    for r in inside[:25]:
        print(f"  d={r['depth']:<3} [{r['control_type']:<10}] "
              f"x={r['rect'][0]:5d} y={r['rect'][1]:5d} w={r['w']:5d} h={r['h']:4d} {r['name'][:34]}")

    OUT.write_text(json.dumps(
        {"dpi": dpi, "window": win, "nodes": len(rows), "named": len(named),
         "with_rect": len(with_rect), "elapsed_ms": round(elapsed, 1),
         "list_box": list_box, "inside_list": inside, "all_named": named},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
