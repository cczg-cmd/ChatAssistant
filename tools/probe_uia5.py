#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/probe_uia5.py - 性能与可用性：只遍历"消息列表"子树，连续多次，统计耗时/消息数/左右判定。

回答三件事：① 只走消息列表子树要多少 ms（能不能 200-500ms 轮询一次）；
            ② 每条消息能不能判出"对方(左)/自己(右)"；
            ③ 连续读取是否稳定（条数、文本是否抖动）。
结果写 tmp/probe_uia5.json（UTF-8）。
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

OUT = WORKSPACE / "tmp" / "probe_uia5.json"
user32 = ctypes.WinDLL("user32", use_last_error=True)
ROUNDS = 8


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
            best = {"hwnd": hwnd, "rect": rect, "area": area, "title": wgc.get_window_text(hwnd)}
    return best


def children(node):
    """返回 [(控件类型, 文本, automation_id, (l,t,r,b))]，失败元素跳过。"""
    out = []
    try:
        kids = node.children()
    except Exception:
        return out
    for kid in kids:
        try:
            info = kid.element_info
            rect = kid.rectangle()
            out.append((info.control_type or "", (info.name or "").strip(),
                        info.automation_id or "", (rect.left, rect.top, rect.right, rect.bottom)))
        except Exception:
            continue
    return out


def read_messages(wrapper) -> tuple[list, float]:
    """遍历整棵树（浅树，~120 节点），取「消息列表」容器内的 Text 节点作为消息。"""
    t0 = time.perf_counter()
    nodes: list[tuple[str, str, tuple]] = []
    stack = [wrapper]
    while stack:
        node = stack.pop()
        for type_, name, _aid, rect in children(node):
            nodes.append((type_, name, rect))
        try:
            stack.extend(node.children())
        except Exception:
            pass
        if len(nodes) > 4000:  # 防御：树异常膨胀时收手
            break
    messages, list_box = [], None
    for type_, name, rect in nodes:
        if name == "消息列表" and rect[3] > rect[1]:
            list_box = rect
            break
    if list_box is None:
        return [], (time.perf_counter() - t0) * 1000
    x0, y0, x1, y1 = list_box
    width = x1 - x0
    for type_, name, rect in nodes:
        if not (x0 <= rect[0] and rect[2] <= x1 and y0 <= rect[1] and rect[3] <= y1):
            continue
        if type_ != "Text" or not name or len(name) < 2:
            continue
        left_edge = rect[0] - x0
        w = rect[2] - rect[0]
        side = "left" if (left_edge < width * 0.35 and w < width * 0.75) else "right"
        messages.append({"text": name, "side": side, "rect": list(rect),
                         "left_offset": left_edge, "width": w})
    messages.sort(key=lambda m: (m["rect"][1], m["rect"][0]))
    return messages, (time.perf_counter() - t0) * 1000


def main() -> int:
    wgc.set_dpi_awareness()
    win = pick_chat_window()
    if not win:
        print("没找到可见的 QQ 聊天窗口")
        return 1
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    wrapper = UIAWrapper(UIAElementInfo(win["hwnd"]))
    # 预热一次（Chromium 的无障碍树是按需构建的，第一次可能拿不到）
    for _ in range(2):
        read_messages(wrapper)

    rounds = []
    for i in range(ROUNDS):
        msgs, ms = read_messages(wrapper)
        rounds.append({"round": i + 1, "ms": round(ms, 1), "count": len(msgs),
                       "left": sum(1 for m in msgs if m["side"] == "left"),
                       "right": sum(1 for m in msgs if m["side"] == "right"),
                       "texts": [m["text"] for m in msgs]})
        time.sleep(0.3)

    times = [r["ms"] for r in rounds]
    counts = {r["count"] for r in rounds}
    texts_stable = len({tuple(r["texts"]) for r in rounds}) == 1
    print(f"窗口 hwnd={win['hwnd']} rect={win['rect']} title={win['title'][:24]!r}")
    print(f"每轮耗时 ms: {times}")
    print(f"平均 {sum(times)/len(times):.1f} ms / 最小 {min(times):.1f} / 最大 {max(times):.1f}")
    print(f"消息条数集合: {sorted(counts)}；连续 8 轮文本完全一致: {texts_stable}")
    last = rounds[-1]
    print(f"最后一轮：左(对方) {last['left']} 条 / 右(自己) {last['right']} 条")
    for text in last["texts"]:
        print("   ", text[:38])
    OUT.write_text(json.dumps({"window": win, "rounds": rounds,
                               "avg_ms": round(sum(times) / len(times), 1),
                               "texts_stable": texts_stable}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print("已写:", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
