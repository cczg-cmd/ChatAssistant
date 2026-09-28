#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 79 轮回归：点输入框的鼠标注入要"抗手抖/抗正在移动鼠标"。

真实问题（用户："点击的同时移动了鼠标就很容易没自动填进去"）：旧实现 MOVE 之后要用
`GetCursorPos` 确认光标**停在**目标点（±6px），用户手还在动就确认不到 → 直接放弃点击
→ 输入框没拿到焦点 → Ctrl+V 打空。现在改成：
  · MOVE + LEFTDOWN + LEFTUP **一个批次**投递（中间插不进真实移动）；
  · 落点由批次里的绝对坐标决定，与"当前光标在哪"无关；
  · 只有 SendInput 一个都没投递（被 UIPI 拦）才失败，并补一次 LEFTUP 安全网。

本测试把 `SendInput` 换成计数器（**不注入任何真实输入**）：
  A. 批次 3/3 投递成功 → 返回 True、不补 LEFTUP；
  B. 批次只成功 2/3 → 返回 False、**补一次 LEFTUP**（防左键卡住）；
  C. 用户此刻正按着左键 → 完全不注入（不打断他的拖拽）。

用法：python tools/test_click_at.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402


def run_case(batch_result: int, left_button_down: bool):
    calls = {"batch": 0, "single": []}
    saved = (w32._send_mouse_batch, w32._send_mouse, w32.is_key_down)

    def fake_batch(events):
        calls["batch"] += 1
        calls["batch_events"] = list(events)
        return batch_result

    def fake_single(flags, abs_x, abs_y):
        calls["single"].append(flags)
        return 1

    w32._send_mouse_batch = fake_batch                 # type: ignore[assignment]
    w32._send_mouse = fake_single                      # type: ignore[assignment]
    w32.is_key_down = lambda vk: left_button_down      # type: ignore[assignment]
    try:
        ok = w32.click_at(500, 400)
    finally:
        w32._send_mouse_batch, w32._send_mouse, w32.is_key_down = saved   # type: ignore[assignment]
    return ok, calls


def main() -> int:
    # A0（第 79 轮真事故）：批次调用的 cbSize 必须是**单个 INPUT 的大小**，
    # 传成数组总长会 err=87 → 点击完全投递不出去（"点选项没反应"）。
    import ctypes as _ctypes

    captured = {}
    real_user32 = w32.user32

    class _Wrapper:
        """只替换 SendInput，其余 API 转发给真的 user32（click_at 还要算虚拟桌面）。"""

        def SendInput(self, count, _array, size):
            captured["count"], captured["size"] = count, size
            return count

        def __getattr__(self, name):
            return getattr(real_user32, name)

    w32.user32 = _Wrapper()                 # type: ignore[assignment]
    try:
        w32.click_at(500, 400)
    finally:
        w32.user32 = real_user32            # type: ignore[assignment]
    one = _ctypes.sizeof(w32._INPUT_MOUSE)
    good_size = (captured.get("count") == 3 and captured.get("size") == one)
    print(f"A0 批次 cbSize=单个 INPUT（{one} 字节）→ {'对' if good_size else '错'}"
          f"（count={captured.get('count')}, size={captured.get('size')}）")

    ok_a, calls_a = run_case(batch_result=3, left_button_down=False)
    flags = [f for f, _x, _y in calls_a.get("batch_events", [])]
    down = any(f & w32.MOUSEEVENTF_LEFTDOWN for f in flags)
    up = any(f & w32.MOUSEEVENTF_LEFTUP for f in flags)
    move = any(f & w32.MOUSEEVENTF_MOVE for f in flags)
    good_a = ok_a and move and down and up and not calls_a["single"]
    print(f"A 批次一次投递 move+down+up → {'对' if good_a else '错'}"
          f"（返回 {ok_a}，批次事件 {len(flags)} 个，补抬起 {len(calls_a['single'])} 次）")

    ok_b, calls_b = run_case(batch_result=2, left_button_down=False)
    good_b = (ok_b is False) and len(calls_b["single"]) == 1
    print(f"B 批次只成功 2/3 → {'对' if good_b else '错'}"
          f"（返回 {ok_b}，补抬起 {len(calls_b['single'])} 次，期望 1）")

    ok_c, calls_c = run_case(batch_result=3, left_button_down=True)
    good_c = (ok_c is False) and calls_c["batch"] == 0
    print(f"C 用户正按着左键（拖拽中）→ {'对' if good_c else '错'}"
          f"（返回 {ok_c}，批次调用 {calls_c['batch']} 次，期望 0）")

    results = [good_size, good_a, good_b, good_c]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
