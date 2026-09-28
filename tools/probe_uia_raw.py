#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读探针：把消息列表里的**原始 UIA 信息**打出来，为"顺序身份"找依据。

重点看这些属性（若 QQ 暴露，就能拿到稳定顺序）：
  PositionInSet(30152) / SizeOfSet(30153) —— "第 k 条 / 共 n 条"
  RuntimeId —— 元素存活期内唯一
  ItemStatus(30010) / ItemType(30011) / LocalizedControlType(30004)
  AriaRole(30101) / AriaProperties(30102)
再打印父子结构（谁是谁的父节点、在父节点里的第几个孩子），判断消息是否挂在
一个 List/ListItem 结构里 —— 若有，孩子的下标就是天然的顺序身份。
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

PROPS = {
    "RuntimeId": 30000, "LocalizedControlType": 30004, "ItemStatus": 30010,
    "ItemType": 30011, "AriaRole": 30101, "AriaProperties": 30102,
    "PositionInSet": 30152, "SizeOfSet": 30153,
}


def read_prop(element, prop_id: int):
    try:
        value = element.GetCurrentPropertyValue(prop_id, True)   # True = 返回默认值而不是抛异常
        if isinstance(value, ctypes.c_void_p) or value is None:
            return None
        text = str(value)
        return text if text and text != "None" else None
    except Exception:
        return None


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
    boxes = wrapper.descendants(control_type="Window", title=cfg.uia.message_list_title)
    if not boxes:
        print("没找到消息列表容器")
        return 1
    container = boxes[0]
    print(f"消息列表容器：control_type={container.element_info.control_type!r} "
          f"name={container.element_info.name!r}")
    # 容器自己的祖先链（看它是否挂在 List/ListItem 结构里）
    ancestors, node = [], container
    for _ in range(5):
        node = node.parent()
        if node is None:
            break
        ancestors.append(f"{node.element_info.control_type}({(node.element_info.name or '')[:14]})")
    print("祖先链（自上而下）：", " ← ".join(ancestors))

    print("\n== 直接子节点（看结构：消息是不是 ListItem / 有没有下标）==")
    try:
        children = container.children()
    except Exception as exc:
        children = []
        print("  取子节点失败:", exc)
    for index, child in enumerate(children[:15]):
        info = child.element_info
        rect = child.rectangle()
        print(f"  child[{index}] type={info.control_type!r} name={(info.name or '')[:20]!r} "
              f"rect=({rect.left},{rect.top},{rect.right},{rect.bottom})")

    print("\n== 消息列表内的节点（含关键属性）==")
    for control_type in ("ListItem", "Group", "Text", "Image"):
        try:
            nodes = container.descendants(control_type=control_type)
        except Exception as exc:
            print(f"  {control_type}: 遍历失败 {exc}")
            continue
        printed = 0
        for node in nodes:
            rect = node.rectangle()
            if rect.right <= rect.left or rect.bottom <= rect.top:
                continue
            element = node.element_info.element
            props = {name: read_prop(element, pid) for name, pid in PROPS.items()}
            if control_type == "Text" and not props.get("PositionInSet"):
                continue                    # Text 太多，没顺序信息的就不打
            parent = node.parent()
            parent_index = "?"
            try:
                if parent is not None:
                    parent_index = next(
                        (i for i, c in enumerate(parent.children())
                         if c.element_info.runtime_id == node.element_info.runtime_id), "?")
            except Exception:
                pass
            print(f"  [{control_type}] {(node.element_info.name or '')[:22]!r} "
                  f"rect=({rect.left},{rect.top}) 父={getattr(parent.element_info, 'control_type', None)} "
                  f"父内序号={parent_index} "
                  f"PositionInSet={props.get('PositionInSet')} SizeOfSet={props.get('SizeOfSet')} "
                  f"ItemStatus={props.get('ItemStatus')} ItemType={props.get('ItemType')} "
                  f"AriaRole={props.get('AriaRole')} RuntimeId={props.get('RuntimeId')}")
            printed += 1
            if printed >= 12:
                break
        print(f"  （{control_type} 共 {len(nodes)} 个，上面列了 {printed} 个）")
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
