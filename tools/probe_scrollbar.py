#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：找"能在滚动过程中实时更新"的信号（不装任何输入钩子）。

思路：消息文本的 UIA 矩形在连续滚动时不更新（已实测），但滚动条（ScrollBar/Thumb）
属于即时 UI，可能每帧都更新。若它的位置/取值实时变化，就能用它算消息位移。
本探针只读控件树 + 只在测试里注入滚轮（不注入按键，不会留下按下状态）。
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
WHEEL = 0x0800


def rect_of(node):
    try:
        r = node.rectangle()
        return [r.left, r.top, r.right, r.bottom]
    except Exception:
        return None


def value_of(node):
    out = {}
    for name, attr in (("value", "get_value"), ("range", "get_value")):
        pass
    try:
        out["value"] = node.iface_value.CurrentValue
    except Exception:
        pass
    try:
        out["range_value"] = node.iface_range_value.CurrentValue
        out["range_max"] = node.iface_range_value.CurrentMaximum
        out["range_min"] = node.iface_range_value.CurrentMinimum
    except Exception:
        pass
    return out


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
    wrapper = reader.uia._wrapper
    hwnd = int((snapshot.get("window") or {})["hwnd"])
    print("=== 查找滚动条元素 ===")
    found = []
    for control_type in ("ScrollBar", "Thumb", "Pane"):
        try:
            nodes = wrapper.descendants(control_type=control_type)
        except Exception as exc:
            print(f"  {control_type}: 遍历失败 {exc}")
            continue
        for node in nodes:
            rect = rect_of(node)
            if rect is None:
                continue
            if control_type == "Pane" and (rect[2] - rect[0]) > 60:
                continue                     # Pane 只关心细条（可能是滚动条容器）
            info = value_of(node)
            name = (node.element_info.name or "").strip()
            found.append((control_type, name, rect, info))
            if len(found) <= 12:
                print(f"  {control_type:9} name={name[:14]!r} rect={rect} {info}")
    print(f"合计候选 {len(found)} 个")

    bars = [f for f in found if f[0] in ("ScrollBar", "Thumb")]
    if not bars:
        print("没有 ScrollBar/Thumb 元素 → 这条路线不通")
        reader.close()
        return 0
    # 取最靠右、最高的那个当"聊天区滚动条"
    bars.sort(key=lambda f: (f[2][0], f[2][1]))
    target_bar = bars[-1]
    print(f"\n选用：{target_bar[0]} {target_bar[1]!r} rect={target_bar[2]} {target_bar[3]}")
    target_msg = next((m for m in messages if m.side == "left" and not m.is_image), None)
    key = target_msg.msg_key if target_msg else messages[0].msg_key
    w32.bring_window_to_front(hwnd)
    time.sleep(0.4)
    if target_msg is not None:
        user32.SetCursorPos(int((target_msg.bbox[0] + target_msg.bbox[2]) // 2),
                            int((target_msg.bbox[1] + target_msg.bbox[3]) // 2))
    time.sleep(0.3)
    print("\n开始连续注入滚轮（每格间隔 50ms），每次同时读：滚动条 + 消息坐标")
    for index in range(6):
        user32.mouse_event(WHEEL, 0, 0, 120, 0)
        time.sleep(0.05)
        # 重新按类型定位滚动条元素（避免句柄失效）
        try:
            nodes = wrapper.descendants(control_type=target_bar[0])
        except Exception:
            nodes = []
        bar_rect, bar_val = None, {}
        for node in nodes:
            r = rect_of(node)
            if r == target_bar[2]:
                bar_rect, bar_val = r, value_of(node)
                break
        msg_rect = reader.refresh_one(key)
        print(f"  {index + 1} 滚动条rect={bar_rect} {bar_val}｜消息y="
              f"{None if msg_rect is None else msg_rect[1]}")
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
