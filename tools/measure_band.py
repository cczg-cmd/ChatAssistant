#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性测量脚本（只读，不注入、不点 QQ）：
量出"同排右侧空白带"在真实 QQ 窗口里到底有多宽 —— 日志里看到的 210 物理宽度
可能只是面板自己宽度被压窄后的假象（宽度棘轮 bug），必须实测 available。
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()          # 必须先做，否则坐标是虚拟化后的逻辑像素

import config  # noqa: E402
from core.message_reader import MessageReader  # noqa: E402

PANEL_H = 180                    # 面板物理高度（120 逻辑 @150%）


def band_for(messages, list_box, window_rect, y0: int, y1: int) -> dict:
    bounds = list_box or window_rect
    gap = 12

    def overlap(rect):
        return not (rect[3] <= y0 or rect[1] >= y1)

    lefts = [m.bbox for m in messages if m.side == "left" and overlap(m.bbox)]
    rights = [m.bbox for m in messages if m.side == "right" and overlap(m.bbox)]
    left_col_right = max((r[2] for r in lefts), default=bounds[0])
    right_col_left = min((r[0] for r in rights), default=bounds[2])
    start = left_col_right + gap
    available = (right_col_left - gap) - start
    return {"start": start, "available": available, "left_col_right": left_col_right,
            "right_col_left": right_col_left, "bounds": bounds,
            "window_right_room": None}


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    cfg = config.load_config()
    reader = MessageReader(cfg)
    messages, snapshot = [], {}
    for attempt in range(12):
        messages, snapshot = reader.read()
        if messages:
            break
        time.sleep(1.5)
    window = snapshot.get("window") or {}
    if not window.get("rect"):
        print(f"读不到 QQ 窗口：status={snapshot.get('status')} err={snapshot.get('last_error')}")
        return 1
    window_rect = list(window["rect"])
    list_box = (snapshot.get("uia") or {}).get("list_box")
    scale = w32.get_window_scale(int(window["hwnd"])) or 1.0
    screen = w32.get_virtual_screen_rect()
    print(f"状态={snapshot.get('status')}｜读取耗时={snapshot.get('last_read_ms')}ms")
    print(f"屏幕（物理）={screen}")
    print(f"QQ 窗口（物理）={window_rect}｜缩放={scale}"
          f"｜逻辑={int((window_rect[2]-window_rect[0])/scale)}x{int((window_rect[3]-window_rect[1])/scale)}")
    print(f"消息列表（物理）={list_box}")
    print(f"窗口右侧剩余（物理）={screen[2] - window_rect[2]}"
          f" → 逻辑 {int((screen[2] - window_rect[2]) / scale)}")
    print(f"\n可见消息 {len(messages)} 条：")
    for msg in messages:
        wide = msg.bbox[2] - msg.bbox[0]
        print(f"  [{msg.side:5}] y={msg.bbox[1]:>5}..{msg.bbox[3]:<5} x={msg.bbox[0]:>5}..{msg.bbox[2]:<5}"
              f"(宽{wide:>4}) {msg.text[:22]!r}")

    print("\n每条对方消息对应面板纵向跨度内的空白带（物理）：")
    rows = []
    for msg in messages:
        if msg.side != "left":
            continue
        center = (msg.bbox[1] + msg.bbox[3]) // 2
        bounds = list_box or window_rect
        top = max(bounds[1], min(center - PANEL_H // 2, bounds[3] - PANEL_H))
        info = band_for(messages, list_box, window_rect, top, top + PANEL_H)
        rows.append(info["available"])
        print(f"  y={top:>5}..{top+PANEL_H:<5} 空白带宽={info['available']:>5} 物理"
              f"（{info['available']/scale:>6.0f} 逻辑）起始 x={info['start']:>5}"
              f"｜左列右缘={info['left_col_right']} 右列左缘={info['right_col_left']}"
              f"｜{msg.text[:16]!r}")
    if rows:
        print(f"\n空白带宽度：最小 {min(rows)} / 中位 {sorted(rows)[len(rows)//2]} / 最大 {max(rows)}（物理）"
              f" → 逻辑 {min(rows)/scale:.0f} / {sorted(rows)[len(rows)//2]/scale:.0f} / {max(rows)/scale:.0f}")
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
