#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/probe_uia7.py - 诊断：Chromium 无障碍树什么时候会被激活？

假设：Chromium 的无障碍是"按需构建 + 空闲超时销毁"。本探针做三件事：
  1. 列出 QQ 顶层窗口的所有子窗口（找 Chrome_RenderWidgetHostHWND 这类渲染窗口）；
  2. 对渲染窗口显式发 WM_GETOBJECT(UiaRootObjectId)，看是否能把树"叫醒"；
  3. 在 ~12 秒内每 1 秒统计一次 UIA 节点数/有名字数，画出激活曲线。
结果写 tmp/probe_uia7.json（UTF-8）。
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

OUT = WORKSPACE / "tmp" / "probe_uia7.json"
user32 = ctypes.WinDLL("user32", use_last_error=True)
UiaRootObjectId = -25
WM_GETOBJECT = 0x003D


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
            best = {"hwnd": hwnd, "rect": list(rect), "area": area,
                    "title": wgc.get_window_text(hwnd)}
    return best


def child_windows(hwnd: int) -> list[dict]:
    out = []
    buf = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(child, _lparam):
        try:
            rect = wgc.get_window_rect(child)
            out.append({"hwnd": int(child), "class": wgc.get_class_name(child),
                        "visible": bool(user32.IsWindowVisible(child)),
                        "rect": list(rect)})
        except Exception:
            pass
        return True

    user32.EnumChildWindows(hwnd, _cb, None)
    return out


def poke_uia_root(hwnd: int) -> int:
    """显式请求 UIA 根对象（UIA 客户端本来就会发这个），返回响应长度。"""
    res = ctypes.c_ulong(0)
    try:
        ok = user32.SendMessageTimeoutW(hwnd, WM_GETOBJECT, 0,
                                       ctypes.c_void_p(UiaRootObjectId), 0x0002, 1500,
                                       ctypes.byref(res))
        return int(res.value) if ok else -1
    except Exception:
        return -2


def count_nodes(wrapper) -> tuple[int, int]:
    try:
        rows = []
        stack = [wrapper]
        while stack and len(rows) < 4000:
            node = stack.pop()
            try:
                rows.append(node)
                stack.extend(node.children())
            except Exception:
                continue
        named = 0
        for n in rows:
            try:
                if (n.element_info.name or "").strip():
                    named += 1
            except Exception:
                continue
        return len(rows), named
    except Exception:
        return 0, 0


def main() -> int:
    wgc.set_dpi_awareness()
    win = pick_chat_window()
    if not win:
        print("没找到可见的 QQ 聊天窗口")
        return 1
    print("QQ 窗口:", win)

    kids = child_windows(win["hwnd"])
    print(f"子窗口 {len(kids)} 个：")
    for k in kids[:12]:
        print("   ", k)

    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    wrapper = UIAWrapper(UIAElementInfo(win["hwnd"]))
    timeline = []
    t0 = time.time()
    for i in range(12):
        nodes, named = count_nodes(wrapper)
        timeline.append({"t": round(time.time() - t0, 2), "nodes": nodes, "named": named,
                         "top_children": len(wrapper.children())})
        print(f"  t={timeline[-1]['t']:5.2f}s 节点={nodes:4d} 有名字={named:3d} "
              f"顶层子节点={timeline[-1]['top_children']}")
        if nodes >= 50:
            break
        # 每秒主动"叫醒"一次：先问顶层，再问渲染子窗口
        poke_uia_root(win["hwnd"])
        for k in kids:
            if "Render" in k["class"] or "Chrome" in k["class"]:
                poke_uia_root(k["hwnd"])
        time.sleep(1.0)

    OUT.write_text(json.dumps({"window": win, "children": kids, "timeline": timeline},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
