#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：验证"消息行的子节点下标"能不能当顺序身份。

背景：`probe_uia_raw.py` 显示"消息列表"容器有 ~39 个直接子节点（每行一个 Group），
可见的行有坐标、滚出屏幕的仍在 DOM 里（坐标为 0）。若这个下标准确且稳定，就能用它
给消息排序 —— 文本重复、图片插在中间都不再影响顺序。

本探针做两件事：
  ① 连续两次读取，列出子节点下标 → 名字/坐标，检查下标是否单调、两次是否一致；
  ② 检查 RuntimeId 是否可读（GetRuntimeId），这是另一个潜在身份。
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


def runtime_id(element):
    try:
        ids = element.GetRuntimeId()
        return "-".join(str(v) for v in ids) if ids else None
    except Exception as exc:
        return f"err:{type(exc).__name__}"


def snapshot(container):
    rows = []
    try:
        children = container.children()
    except Exception as exc:
        print("取子节点失败:", exc)
        return rows
    for index, child in enumerate(children):
        info = child.element_info
        rect = child.rectangle()
        rows.append({
            "index": index,
            "type": info.control_type,
            "name": (info.name or "")[:18],
            "y": rect.top,
            "visible": rect.bottom > rect.top,
            "rid": runtime_id(info.element),
        })
    return rows


def main() -> int:
    cfg = app_config.load_config()
    reader = MessageReader(cfg)
    for _ in range(10):
        messages, snapshot_ = reader.read()
        if messages:
            break
        time.sleep(1.5)
    wrapper = reader.uia._wrapper
    boxes = wrapper.descendants(control_type="Window", title=cfg.uia.message_list_title)
    if not boxes:
        print("没找到消息列表容器")
        return 1
    container = boxes[0]
    first = snapshot(container)
    print(f"子节点共 {len(first)} 个｜其中有坐标的（可见行）"
          f"{sum(1 for r in first if r['visible'])} 个")
    print("\n① 下标 → 类型/名字/坐标（只看可见行，检查下标与 y 是否同序）：")
    last_y, ordered = -10**9, True
    for row in first:
        if not row["visible"]:
            continue
        if row["y"] < last_y:
            ordered = False
        last_y = row["y"]
        print(f"   [{row['index']:>3}] {row['type']:<8} y={row['y']:>5} {row['name']!r} "
              f"RuntimeId={row['rid']}")
    print(f"   → 下标顺序与 y 顺序一致：{'是' if ordered else '否'}")
    time.sleep(2.0)
    second = snapshot(container)
    same = [(a, b) for a, b in zip(first, second) if a["name"] == b["name"]
            and a["type"] == b["type"]]
    drift = [(a["index"], b["index"], a["name"]) for a, b in same if a["index"] != b["index"]]
    print(f"\n② 2 秒后再读一次：子节点 {len(second)} 个｜同名同类型条目 {len(same)} 个"
          f"｜下标发生变化的 {len(drift)} 个")
    for before, after, name in drift[:8]:
        print(f"   下标 {before} → {after}（{name!r}）")
    rid_ok = sum(1 for r in first if r["rid"] and not str(r["rid"]).startswith("err"))
    print(f"\n③ RuntimeId 可读的条目：{rid_ok}/{len(first)}")
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
