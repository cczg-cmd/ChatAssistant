#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 80 轮回归：**同一段文本出现多条时，点击哪条就分析哪条**（纯离线，不加载模型）。

用户实测："如果我和对方发了同样的文本，我点击下面发的那条只会被判定成点击上面那条"。
根因两处：
  ① `make_msg_key(会话, 文本)` 不含侧别 → 对方一条"在吗"、我也一条"在吗"共用同一个 key
     （缓存/上下文/命中折算全串在一起）；
  ② `_original_message(key)` 无脑返回**第一条**同 key 的消息 → 点下面那条被折算成上面那条。
修法：① key 里加侧别；② `_original_message(key, near_bbox)` 取"与这一击坐标最接近"的那条
     （坐标用与命中测试同一套"平移后"口径）。

用法：python tools/test_dup_text_click.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main as app_main  # noqa: E402
from core.message_reader import Message, make_msg_key, normalize_text  # noqa: E402

SAME = "在吗"
UP = Message(msg_key=make_msg_key("s", SAME, "left"), text=SAME, side="left",
             bbox=(400, 300, 500, 340))
DOWN = Message(msg_key=make_msg_key("s", SAME, "right"), text=SAME, side="right",
               bbox=(900, 520, 1000, 560))
# 同侧同文本再来一条（key 会撞，属于已知边界：靠坐标区分）
UP2 = Message(msg_key=make_msg_key("s", SAME, "left"), text=SAME, side="left",
              bbox=(400, 700, 500, 740))
OTHER = Message(msg_key=make_msg_key("s", "吃饭没", "left"), text="吃饭没", side="left",
                bbox=(400, 400, 500, 440))


class StubApp:
    """只实现 `_original_message` / `_translated_messages` 需要的字段。"""

    _original_message = app_main.ChatAssistantApp._original_message
    _translated_messages = app_main.ChatAssistantApp._translated_messages
    _translated_bbox_of = app_main.ChatAssistantApp._translated_bbox_of

    def __init__(self, read_rect=None, window_rect=None) -> None:
        self.messages = [UP, DOWN, UP2, OTHER]
        self._read_window_rect = read_rect
        self.window_rect = window_rect


def case_key_has_side() -> bool:
    """A：key 带侧别 —— 左右同文本不同 key；同侧同文本同 key；两参调用保持兼容。"""
    left = make_msg_key("s", SAME, "left")
    right = make_msg_key("s", SAME, "right")
    again = make_msg_key("s", " " + SAME + " ", "left")      # 归一化后应一致
    legacy = make_msg_key("s", SAME)
    ok = (left != right) and (left == again) and (legacy not in (left, right))
    print(f"A key 带侧别（左右不同、归一化一致、两参兼容）：{'对' if ok else '错'}"
          f"（left={left} right={right}）")
    return ok


def case_click_resolves_nearest() -> bool:
    """B：点哪条取回哪条（含同侧重复：按坐标取最近的）。"""
    stub = StubApp()
    got_down = stub._original_message(DOWN.msg_key, DOWN.bbox)      # noqa: SLF001
    got_up = stub._original_message(UP.msg_key, UP.bbox)            # noqa: SLF001
    got_up2 = stub._original_message(UP2.msg_key, UP2.bbox)         # noqa: SLF001
    old = stub._original_message(UP.msg_key)                        # noqa: SLF001
    ok = (got_down is DOWN and got_up is UP and got_up2 is UP2 and old is UP)
    print(f"B 点哪条取哪条（同侧重复按坐标）：{'对' if ok else '错'}"
          f"（下={getattr(got_down, 'side', None)} 上={getattr(got_up, 'bbox', None)}"
          f" 同侧第二条={'命中' if got_up2 is UP2 else '错'}）")
    return ok


def case_window_shift() -> bool:
    """C：窗口移动过（坐标平移）时仍取对——用"平移后"口径比对。"""
    read = (100, 100, 1500, 900)
    moved = (120, 160, 1520, 960)                 # 窗口右移 20、下移 60
    stub = StubApp(read_rect=read, window_rect=moved)
    translated = stub._translated_messages()      # noqa: SLF001
    hit_down = next(m for m in translated if m.side == "right" and m.bbox[1] < 600)
    got = stub._original_message(hit_down.msg_key, hit_down.bbox)   # noqa: SLF001
    got_up = stub._original_message(UP.msg_key, translated[0].bbox)  # noqa: SLF001
    ok = (got is DOWN) and (got_up is UP)
    print(f"C 窗口平移后仍取对：{'对' if ok else '错'}"
          f"（平移 dx/dy={stub.window_rect[0] - read[0]}/{stub.window_rect[1] - read[1]}）")
    return ok


def case_anchor_uses_translated_target() -> bool:
    """D：锚点换算 `_translated_bbox_of` —— 给目标消息能取回它自己（而不是重复里的第一条）。"""
    read = (100, 100, 1500, 900)
    moved = (140, 100, 1540, 900)                 # 只横向移动
    stub = StubApp(read_rect=read, window_rect=moved)
    near = stub._translated_bbox_of(DOWN)         # noqa: SLF001
    got = stub._original_message(DOWN.msg_key, near)   # noqa: SLF001
    elsewhere = stub._translated_bbox_of(OTHER)   # noqa: SLF001
    ok = (got is DOWN) and (elsewhere != near)
    print(f"D 锚点用平移后坐标取回目标自己：{'对' if ok else '错'}")
    return ok


def main() -> int:
    results = [case_key_has_side(), case_click_resolves_nearest(),
               case_window_shift(), case_anchor_uses_translated_target()]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
