#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 68d 轮回归：快速拖动 QQ 窗口时，"过期的 UIA 读取"不能让状态灯闪回旧位置。

机制：Chromium 快速拖动时不更新无障碍矩形（UIA 坐标还停在拖动前），而 window["rect"]
是 Win32 实时值。两者混进 (list_box, _read_window_rect) 这对基准后，dx ≈ 0，
浮窗就会按过期绝对坐标摆回拖动前的位置 → 肉眼"闪"。

用法：python tmp/test_dot_drag_freeze.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import config as app_config  # noqa: E402
from core.message_reader import Message, make_msg_key  # noqa: E402
from main import ChatAssistantApp  # noqa: E402

R0 = [50, 300, 950, 1200]          # 拖动前的窗口矩形（UIA 那批坐标对应的窗口位置）
L0 = [100, 400, 900, 1000]         # 拖动前的消息列表矩形
R1 = [600, 300, 1500, 1200]        # 拖动后（+550）的实时窗口矩形
L1 = [650, 400, 1450, 1000]        # 停手后 UIA 追上的新列表矩形


def snapshot(rect, list_box, title="小杰"):
    return {"status": "ready", "last_read_ms": 50.0,
            "window": {"hwnd": 123, "pid": 456, "title": title, "rect": rect,
                       "iconic": False},
            "uia": {"available": True, "nodes": 60, "named": 20, "activation_s": 0.0,
                    "list_box": list_box}}


def main() -> int:
    app_qt = QApplication(sys.argv)                     # noqa: F841
    cfg = app_config.load_config()
    gui = ChatAssistantApp(app_qt, cfg)
    results = []

    # 拖动前：一次正常读取
    gui.window_rect = list(R0)
    gui.list_box = list(L0)
    gui._read_window_rect = list(R0)
    gui.scale = 1.0
    msg = Message(msg_key=make_msg_key("s", "一条消息"), text="一条消息", side="left",
                  bbox=(200, 700, 700, 760))
    gui.on_messages([msg], snapshot(R0, L0))
    dot0 = gui._dot_physical_rect()
    print(f"拖动前：list_box={gui.list_box}｜read_rect={gui._read_window_rect}｜状态灯={dot0}")
    results.append(dot0[0] == 866 and dot0[1] == 408)

    # 拖动中：窗口已移动 +550，UIA 还是"过期"的旧坐标（Chromium 行为）
    gui._window_moving_until = time.time() + 1.0        # 窗口移动钩子刚触发过
    gui.window_rect = list(R1)
    gui.on_messages([msg], snapshot(R1, L0))           # ← 关键：list_box 是过期的 L0
    dot1 = gui._dot_physical_rect()
    print(f"拖动中（过期 UIA）：状态灯={dot1}（期望 x={866 + 550}=1416，"
          f"即刚性平移；闪回 866 就是 bug）")
    results.append(dot1[0] == 1416)

    # 停手后：UIA 追上（list_box 变成 L1），应重新接纳
    gui._window_moving_until = 0.0
    gui.on_messages([msg], snapshot(R1, L1))
    dot2 = gui._dot_physical_rect()
    print(f"停手后（UIA 追上）：list_box={gui.list_box}｜read_rect={gui._read_window_rect}"
          f"｜状态灯={dot2}")
    results.append(gui.list_box == L1 and gui._read_window_rect == R1 and dot2[0] == 1416)

    print(f"\n{sum(results)}/{len(results)} 通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
