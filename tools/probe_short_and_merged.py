#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：确认两件事
  ① 有没有被 min_message_chars(=2) 丢掉的**单字消息**（"嗯""?"）→ 会让悬停落到相邻消息；
  ② 有没有把**相邻两条并进同一个文本节点**（名字里带换行）→ 会被当成同一条分析。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()

import config as app_config  # noqa: E402
from core.message_reader import MessageReader, normalize_text  # noqa: E402


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
        print(f"读不到消息：status={snapshot.get('status')}")
        return 1
    print(f"当前解析出的消息 {len(messages)} 条（min_message_chars="
          f"{cfg.uia.min_message_chars}）")
    for msg in messages:
        print(f"  [{msg.side:5}] y={msg.bbox[1]:>5}..{msg.bbox[3]:<5} {msg.text[:26]!r}")

    wrapper = reader.uia._wrapper
    boxes = wrapper.descendants(control_type="Window", title=cfg.uia.message_list_title)
    list_box = [boxes[0].rectangle().left, boxes[0].rectangle().top,
                boxes[0].rectangle().right, boxes[0].rectangle().bottom]
    x0, y0, x1, y1 = list_box
    short, merged, total = [], [], 0
    for node in boxes[0].descendants(control_type="Text"):
        name = (node.element_info.name or "").strip()
        rect = node.rectangle()
        rect = [rect.left, rect.top, rect.right, rect.bottom]
        if not name or rect[2] <= rect[0] or rect[3] <= rect[1]:
            continue
        if not (x0 <= rect[0] and rect[2] <= x1 and y0 <= rect[1] and rect[3] <= y1):
            continue
        total += 1
        norm = normalize_text(name)
        if len(norm) < cfg.uia.min_message_chars:
            short.append((norm, rect))
        if "\n" in name or "\r" in name:
            merged.append((name, rect))
    print(f"\n消息列表内可见文本节点 {total} 个")
    print(f"① 会因 min_message_chars 被丢弃的短文本：{len(short)} 个")
    for text, rect in short[:10]:
        print(f"   {text!r} rect={rect}")
    print(f"② 名字里带换行（疑似把相邻消息并成一个节点）：{len(merged)} 个")
    for text, rect in merged[:5]:
        print(f"   {text[:60]!r} rect={rect}")
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
