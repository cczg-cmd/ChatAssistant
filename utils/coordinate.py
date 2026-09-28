#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""utils/coordinate.py - 物理/逻辑坐标与矩形工具（SPEC 5.7）

本机是 150% 缩放单屏：Win32 拿到的都是物理像素，本模块只做纯几何换算，不查询窗口。
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

Rect = Tuple[int, int, int, int]


def rect_size(rect: Sequence[int]) -> Tuple[int, int]:
    return (max(0, int(rect[2] - rect[0])), max(0, int(rect[3] - rect[1])))


def rect_area(rect: Sequence[int]) -> int:
    w, h = rect_size(rect)
    return w * h


def center(rect: Sequence[int]) -> Tuple[int, int]:
    return (int((rect[0] + rect[2]) / 2), int((rect[1] + rect[3]) / 2))


def intersects(a: Sequence[int], b: Sequence[int]) -> bool:
    """矩形是否相交（用于"面板有没有压到消息"的判定）。"""
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def contains(outer: Sequence[int], inner: Sequence[int]) -> bool:
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and inner[2] <= outer[2] and inner[3] <= outer[3])


def point_in_rect(point: Sequence[int], rect: Sequence[int], padding: int = 0) -> bool:
    x, y = point
    return (rect[0] - padding <= x <= rect[2] + padding
            and rect[1] - padding <= y <= rect[3] + padding)


def clamp_rect(rect: Sequence[int], bounds: Sequence[int]) -> List[int]:
    """把矩形整体夹进 bounds 内（不改变大小，超出时贴边）。"""
    w, h = rect_size(rect)
    left = min(max(int(rect[0]), int(bounds[0])), max(int(bounds[0]), int(bounds[2]) - w))
    top = min(max(int(rect[1]), int(bounds[1])), max(int(bounds[1]), int(bounds[3]) - h))
    return [left, top, left + w, top + h]


def logical_to_physical(value: float, scale: float) -> int:
    return int(round(value * scale))


def physical_to_logical(value: float, scale: float) -> int:
    return int(round(value / scale)) if scale else int(value)


def physical_rect_to_logical(rect: Sequence[int], scale: float) -> List[int]:
    return [physical_to_logical(v, scale) for v in rect]


def logical_rect_to_physical(rect: Sequence[int], scale: float) -> List[int]:
    return [logical_to_physical(v, scale) for v in rect]


def content_gap_band(messages: Sequence[dict], list_box: Sequence[int],
                     near_y: Optional[int] = None, near_span: int = 200,
                     ) -> Optional[Tuple[int, int]]:
    """算出"左侧内容列"与"右侧内容列"之间的空白带 [start, end]（SPEC 4.3）。

    near_y/near_span：只统计目标消息附近（±near_span）的消息 —— 长消息会话里全局左右列
    可能重叠，按附近统计更容易找到同排空白带（第 0 步实测发现的改进点）。
    返回 None 表示空白带不足以放面板（调用方应退到"窗口外侧"或"气泡下方"）。
    """
    def _field(item, name):
        """兼容 Message dataclass 与 dict（两处调用都出现过）。"""
        if isinstance(item, dict):
            return item.get(name)
        return getattr(item, name, None)

    def _in_range(rect: Sequence[int]) -> bool:
        if near_y is None:
            return True
        return abs(((rect[1] + rect[3]) // 2) - near_y) <= near_span

    lefts = [m.bbox if not isinstance(m, dict) else m["bbox"]
             for m in messages if _field(m, "side") == "left"
             and _in_range(_field(m, "bbox"))]
    rights = [m.bbox if not isinstance(m, dict) else m["bbox"]
              for m in messages if _field(m, "side") == "right"
              and _in_range(_field(m, "bbox"))]
    left_col_right = max((r[2] for r in lefts), default=list_box[0])
    right_col_left = min((r[0] for r in rights), default=list_box[2])
    return (left_col_right, right_col_left)


if __name__ == "__main__":
    print(rect_size((0, 0, 100, 50)), intersects((0, 0, 10, 10), (5, 5, 15, 15)),
          point_in_rect((7, 7), (0, 0, 10, 10), 2))
