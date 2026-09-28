#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：消息列表容器是否暴露 UIA ScrollPattern（能不能读到"滚动百分比"）。

动机：滚动时缓存元素矩形全部失效（虚拟化重建 DOM），靠元素矩形无法实时跟随；
若能读到 VerticalScrollPercent，就能用它算出位移量、自己做平滑跟随。
同时顺便验证"滚动中元素矩形是否失效"，并注入滚轮观察百分比变化。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()

import config as app_config  # noqa: E402
from core.message_reader import MessageReader  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
SCROLL_PATTERN_ID = 10004


def wheel(notches: int) -> None:
    for _ in range(abs(notches)):
        user32.mouse_event(0x0800, 0, 0, -120 if notches < 0 else 120, 0)
        time.sleep(0.09)


def read_scroll(element):
    """返回 (vertical_percent, view_size_percent, horizontal_percent) 或 None。"""
    try:
        from comtypes.gen import UIAutomationClient as UIA
        guid = UIA.IUIAutomationScrollPattern._iid_
        pat = element.GetCurrentPatternAs(SCROLL_PATTERN_ID, ctypes.byref(guid))
        return (pat.CurrentVerticalScrollPercent, pat.CurrentVerticalViewSize,
                pat.CurrentHorizontalScrollPercent)
    except Exception as exc:
        return f"err:{type(exc).__name__}: {exc}"


def main() -> int:
    cfg = app_config.load_config()
    reader = MessageReader(cfg)
    messages, snapshot = [], {}
    for _ in range(10):
        messages, snapshot = reader.read()
        if messages:
            break
        time.sleep(1.5)
    if not messages:
        print("读不到消息:", snapshot.get("status"))
        return 1
    backend = reader.uia
    list_el = backend._list_element
    print("消息列表元素:", type(list_el).__name__)
    target = next((m for m in messages if m.side == "left" and not m.is_image), None)
    key = target.msg_key if target else messages[0].msg_key
    hwnd = int((snapshot.get("window") or {})["hwnd"])
    w32.bring_window_to_front(hwnd)
    time.sleep(0.4)
    if target is not None:
        cx = (target.bbox[0] + target.bbox[2]) // 2
        cy = (target.bbox[1] + target.bbox[3]) // 2
        user32.SetCursorPos(int(cx), int(cy))
    time.sleep(0.3)
    elem = getattr(getattr(list_el, "element_info", None), "element", None)
    print("原生元素:", type(elem).__name__)
    print("滚动前 scroll:", read_scroll(elem))
    print("\n边滚边测（每轮先注入 1 格滚轮再立刻读坐标 *并计时*）：")
    for index in range(10):
        t0 = time.perf_counter()
        rect = backend.refresh_one(key)
        ms_one = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        any_hit = backend.refresh_any()
        ms_any = (time.perf_counter() - t1) * 1000
        print(f"  {index:2d} refresh_one={ms_one:7.1f}ms  refresh_any={ms_any:7.1f}ms "
              f"｜目标矩形={rect}｜任一条={None if not any_hit else any_hit[1]}")
        wheel(1)
        time.sleep(0.16)
    print("滚动后 scroll:", read_scroll(elem))
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
