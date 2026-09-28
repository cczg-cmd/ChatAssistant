#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/probe_uia8.py - 激活实验：注册 UIA 事件监听能否让 Chromium 打开无障碍树。

背景：Chromium 用 UiaClientsAreListening() 判断"有没有读屏软件在听"，只有为真时
才会给渲染进程打开无障碍树。本探针：
  1. 打印 UiaClientsAreListening() 当前值、前台窗口是否就是 QQ；
  2. 通过 comtypes 调 IUIAutomation::AddAutomationEventHandler 注册一个
     StructureChanged 事件监听（读屏软件的标准动作），再轮询节点数最多 15s；
  3. 记录激活耗时曲线，最后移除监听。
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

OUT = WORKSPACE / "tmp" / "probe_uia8.json"
user32 = ctypes.WinDLL("user32", use_last_error=True)
uia_core = ctypes.WinDLL("UIAutomationCore.dll")


def clients_are_listening() -> bool:
    try:
        uia_core.UiaClientsAreListening.restype = ctypes.c_bool
        return bool(uia_core.UiaClientsAreListening())
    except Exception:
        return False


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


def count_nodes(wrapper) -> tuple[int, int]:
    rows, stack = [], [wrapper]
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


def main() -> int:
    wgc.set_dpi_awareness()
    result = {"uia_clients_listening_before": clients_are_listening()}
    win = pick_chat_window()
    result["window"] = win
    result["foreground_hwnd"] = int(user32.GetForegroundWindow())
    result["qq_is_foreground"] = bool(win and result["foreground_hwnd"] == win["hwnd"])
    print("UiaClientsAreListening(before) =", result["uia_clients_listening_before"])
    print("前台窗口 =", result["foreground_hwnd"], "QQ =", win and win["hwnd"],
          "是否同一窗口 =", result["qq_is_foreground"])
    if not win:
        print("没找到可见 QQ 聊天窗口")
        return 1

    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    wrapper = UIAWrapper(UIAElementInfo(win["hwnd"]))
    timeline = []

    def sample(tag: str) -> None:
        nodes, named = count_nodes(wrapper)
        timeline.append({"t": tag, "nodes": nodes, "named": named,
                         "listening": clients_are_listening()})
        print(f"  [{tag}] 节点={nodes:4d} 有名字={named:3d} listening={clients_are_listening()}")

    sample("start")

    handler = None
    try:
        import comtypes.client
        from comtypes import COMObject

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        uia = comtypes.client.CreateObject(
            "{FF48DBA4-60EF-4201-AA87-54103EEF594E}",
            interface=UIA.IUIAutomation)
        root = uia.ElementFromHandle(ctypes.c_void_p(win["hwnd"]))

        class Handler(COMObject):
            _com_interfaces_ = [UIA.IUIAutomationEventHandler]

            def HandleAutomationEvent(self, sender, event_id):  # noqa: N802
                return 0

        handler = Handler()
        # StructureChanged：读屏软件常用的事件类型之一
        uia.AddAutomationEventHandler(UIA.UIA_StructureChangedEventId, root,
                                     UIA.TreeScope_Subtree, None, handler)
        result["handler_registered"] = True
        print("已注册 StructureChanged 事件监听（TreeScope_Subtree）")
    except Exception as exc:
        result["handler_registered"] = False
        result["handler_error"] = f"{type(exc).__name__}: {exc}"
        print("注册事件监听失败:", result["handler_error"])

    t0 = time.time()
    activated_at = None
    for _ in range(15):
        time.sleep(1.0)
        sample(f"{time.time()-t0:.0f}s")
        if timeline[-1]["nodes"] >= 50:
            activated_at = round(time.time() - t0, 1)
            break

    # 复用 pywinauto 的 wrapper 也可能缓存了旧信息，重新取一次元素
    try:
        wrapper2 = UIAWrapper(UIAElementInfo(win["hwnd"]))
        nodes, named = count_nodes(wrapper2)
        timeline.append({"t": "fresh_wrapper", "nodes": nodes, "named": named,
                         "listening": clients_are_listening()})
        print(f"  [fresh_wrapper] 节点={nodes} 有名字={named}")
    except Exception as exc:
        print("重新取元素失败:", exc)

    if handler is not None:
        try:
            uia.RemoveAutomationEventHandler(UIA.UIA_StructureChangedEventId, root, handler)
            print("已移除事件监听")
        except Exception as exc:
            print("移除事件监听失败:", exc)

    result["activated_at_s"] = activated_at
    result["timeline"] = timeline
    result["uia_clients_listening_after"] = clients_are_listening()
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
