#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/probe_uia2.py - 对照组：QQ 窗口是否最小化 + 可见的 Chromium 窗口(Edge)能否被 UIA 读到。"""
from __future__ import annotations

import ctypes
import sys
import time
from collections import Counter

WORKSPACE = r"D:\QQChatAssistant"
sys.path.insert(0, rf"{WORKSPACE}\spikes")
import spike_wgc as wgc  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)


def main() -> int:
    wgc.set_dpi_awareness()
    print("=== QQ 顶层窗口状态 ===")
    for hwnd, pid in wgc.enum_top_level_windows():
        try:
            cls = wgc.get_class_name(hwnd)
        except Exception:
            continue
        if cls != "Chrome_WidgetWin_1":
            continue
        exe = ""
        try:
            exe = wgc.get_process_image_path(pid).lower()
        except Exception:
            pass
        if "qq" not in exe and "msedge" not in exe and "chrome" not in exe:
            continue
        title = wgc.get_window_text(hwnd)
        rect = wgc.get_window_rect(hwnd)
        iconic = bool(user32.IsIconic(hwnd))
        vis = bool(user32.IsWindowVisible(hwnd))
        print(f"  hwnd={hwnd} pid={pid} iconic={iconic} visible={vis} rect={rect} "
              f"cls={cls} title={title[:36]!r} exe={exe.split(chr(92))[-1]}")

    print("=== UIA 对照：每个 Chromium 顶层窗口能读到多少节点 ===")
    from pywinauto import Desktop

    for hwnd, pid in wgc.enum_top_level_windows():
        try:
            if wgc.get_class_name(hwnd) != "Chrome_WidgetWin_1":
                continue
        except Exception:
            continue
        try:
            exe = wgc.get_process_image_path(pid).lower()
        except Exception:
            continue
        if not any(tag in exe for tag in ("qq", "msedge", "chrome")):
            continue
        t0 = time.perf_counter()
        try:
            from pywinauto.controls.uiawrapper import UIAWrapper
            from pywinauto.element_info import ElementInfo
            from pywinauto.uia_element_info import UIAElementInfo

            elem = UIAElementInfo(hwnd)
            wrapper = UIAWrapper(elem)
            nodes = wrapper.descendants()
        except Exception as exc:
            print(f"  hwnd={hwnd} 失败: {type(exc).__name__}: {exc}")
            continue
        kinds = Counter()
        named = 0
        for n in nodes:
            try:
                info = n.element_info
                kinds[info.control_type or "?"] += 1
                if (info.name or "").strip():
                    named += 1
            except Exception:
                continue
        print(f"  hwnd={hwnd} exe={exe.split(chr(92))[-1]:<12} nodes={len(nodes):5d} "
              f"named={named:4d} ms={(time.perf_counter()-t0)*1000:6.0f} "
              f"kinds={dict(kinds.most_common(6))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
