#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：QQ 向上滚动时会出现"回到底部"按钮（截图里那个蓝色双箭头）。
如果 UIA 能读到它（或其名称含"底部/最新/未读"），就能精确判断"当前是否停在最新消息"，
从而做"只在真·最新消息时才优先分析它"的更优方案，而不是拿"屏幕最下面那条"当最新。
本脚本只读控件树，不改任何状态。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()

import config  # noqa: E402
from core.message_reader import MessageReader  # noqa: E402

KEYWORDS = ("底部", "最新", "未读", "新消息", "回到", "bottom", "latest", "unread")


def main() -> int:
    cfg = config.load_config()
    reader = MessageReader(cfg)
    messages, snapshot = [], {}
    for _ in range(10):
        messages, snapshot = reader.read()
        if messages or snapshot.get("status") == "ready":
            break
        time.sleep(1.5)
    if snapshot.get("status") != "ready":
        print(f"状态={snapshot.get('status')}，读不到消息，跳过探针")
        return 1
    window_rect = list((snapshot.get("window") or {}).get("rect") or [])
    wrapper = reader.uia._wrapper
    print(f"消息 {len(messages)} 条｜窗口={window_rect}")

    hits, buttons = [], []
    try:
        nodes = wrapper.descendants(control_type="Button")
    except Exception as exc:
        print(f"按钮遍历失败：{exc}")
        nodes = []
    for node in nodes:
        try:
            name = (node.element_info.name or "").strip()
            rect = [node.rectangle().left, node.rectangle().top,
                    node.rectangle().right, node.rectangle().bottom]
        except Exception:
            continue
        if not name:
            continue
        buttons.append((name, rect))
        if any(k in name for k in KEYWORDS):
            hits.append((name, rect))
    print(f"按钮总数={len(buttons)}；关键词命中={len(hits)}")
    for name, rect in hits:
        print(f"  命中：{name!r} rect={rect}")
    print("全部按钮（前 25 个，含坐标）：")
    for name, rect in buttons[:25]:
        print(f"  {name!r} rect={rect}")
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
