#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/probe_uia3.py - 复测：QQ 打开对话界面后，UIA 能否读到消息文本 + bbox。

做法：先用 Win32 枚举 QQ 的顶层 Chromium 窗口（拿到真实 rect / 是否最小化），
再对"可见且未最小化"的窗口做 UIA 遍历，打印节点数、控件类型、以及前若干个有名字的节点（含 bbox）。
对照：同时跑一次 Edge（可见 Chromium），确认探针本身正常。
"""
from __future__ import annotations

import ctypes
import sys
import time
from collections import Counter

WORKSPACE = r"D:\QQChatAssistant"
sys.path.insert(0, rf"{WORKSPACE}\spikes")
import spike_wgc as wgc  # noqa: E402

MAX_NODES = 3000
user32 = ctypes.WinDLL("user32", use_last_error=True)


def collect_windows(tag: str) -> list:
    out = []
    for hwnd, pid in wgc.enum_top_level_windows():
        try:
            if wgc.get_class_name(hwnd) != "Chrome_WidgetWin_1":
                continue
            exe = wgc.get_process_image_path(pid).lower()
        except Exception:
            continue
        if tag not in exe:
            continue
        rect = wgc.get_window_rect(hwnd)
        out.append({
            "hwnd": hwnd, "pid": pid, "exe": exe.split("\\")[-1],
            "title": wgc.get_window_text(hwnd), "rect": rect,
            "iconic": bool(user32.IsIconic(hwnd)),
            "visible": bool(user32.IsWindowVisible(hwnd)),
            "area": max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1]),
        })
    return out


def dump_uia(hwnd: int, label: str) -> dict:
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    t0 = time.perf_counter()
    try:
        wrapper = UIAWrapper(UIAElementInfo(hwnd))
        nodes = wrapper.descendants()
    except Exception as exc:
        print(f"  [{label}] UIA 失败: {type(exc).__name__}: {exc}")
        return {"ok": False, "nodes": 0}
    elapsed = (time.perf_counter() - t0) * 1000

    kinds = Counter()
    named = []
    for node in nodes[:MAX_NODES]:
        try:
            info = node.element_info
            kinds[info.control_type or "?"] += 1
            name = (info.name or "").strip()
            if name:
                named.append((info.control_type, name, node.rectangle()))
        except Exception:
            continue
    print(f"  [{label}] 节点={len(nodes)} 有名字={len(named)} 耗时={elapsed:.0f}ms "
          f"kinds={dict(kinds.most_common(8))}")
    for kind, name, rect in named[:25]:
        print(f"      [{kind:<12}] x={rect.left:5d} y={rect.top:5d} "
              f"w={rect.width():4d} h={rect.height():4d} {name[:42]!r}")
    return {"ok": True, "nodes": len(nodes), "named": len(named), "ms": elapsed,
            "kinds": dict(kinds.most_common(8))}


def main() -> int:
    print("DPI:", wgc.set_dpi_awareness())
    print("=== QQ 顶层 Chromium 窗口 ===")
    qq = collect_windows("qq.exe")
    qqex = collect_windows("qqex.exe")
    for w in qq + qqex:
        print(f"  hwnd={w['hwnd']:>8} {w['exe']:<9} iconic={w['iconic']!s:<5} "
              f"visible={w['visible']!s:<5} area={w['area']:>8} rect={w['rect']} "
              f"title={w['title'][:32]!r}")

    targets = [w for w in qq + qqex if w["visible"] and not w["iconic"] and w["area"] > 200 * 200]
    targets.sort(key=lambda w: -w["area"])
    if not targets:
        print("没有'可见且未最小化'的 QQ 窗口 —— 请确认 QQ 对话窗口已打开并置于前台")
    else:
        print(f"=== UIA 遍历（{len(targets)} 个候选窗口，按面积从大到小） ===")
        for w in targets[:3]:
            dump_uia(w["hwnd"], f"{w['exe']} {w['rect'][2]-w['rect'][0]}x{w['rect'][3]-w['rect'][1]}")

    print("=== 对照：Edge（可见 Chromium） ===")
    edges = [w for w in collect_windows("msedge.exe") if w["visible"] and not w["iconic"]]
    edges.sort(key=lambda w: -w["area"])
    if edges:
        dump_uia(edges[0]["hwnd"], "msedge 对照")
    else:
        print("  没有可见的 Edge 窗口，跳过对照")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
