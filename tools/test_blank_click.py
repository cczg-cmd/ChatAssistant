#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 79 轮续回归：**空白处点击要立刻收起面板**（不要"再点一两下才收回"）。

背景（用户："现在有时候按空白区域面板不会收回，要再点一两下才收回是怎么回事"）：
点击有两条通道 —— 30ms 轮询（`_poll_click`）与 Raw Input（`on_raw_click`）。
第 79 轮为了修"点气泡被过期坐标判成点空白"，把 Raw Input 那条改成了"先挂起这一击、
等新坐标重判（窗口 1.2s）"。结果：只要 Raw Input 先到（它基本总是先到），
一次**真正的空白点击**也走挂起 → 用户看到"没反应"；等他再点一下才立刻收起。
现在两条通道共用一个 `_on_click_miss()`，并按"面板开着与否 + 坐标新鲜度"分流。

用法：python tools/test_blank_click.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
import main as app_main  # noqa: E402
from core.message_reader import Message  # noqa: E402


class StubCapture:
    def __init__(self, paused: bool = False) -> None:
        self._paused = bool(paused)
        self.now_calls = 0
        self.pause_calls: List[bool] = []

    def is_paused(self) -> bool:
        return self._paused

    def set_paused(self, value: bool) -> None:
        self._paused = bool(value)
        self.pause_calls.append(bool(value))

    def request_now(self) -> None:
        self.now_calls += 1


class StubPanel:
    def __init__(self, visible: bool) -> None:
        self._visible = bool(visible)

    def isVisible(self) -> bool:      # noqa: N802
        return self._visible


class StubApp:
    """只实现 `_on_click_miss` / `_retry_pending_click` 用到的那几个接口。"""

    def __init__(self, visible: bool = True, age: float = 0.1,
                 messages: Optional[Any] = None, paused: bool = False,
                 cursor_in_qq: bool = True, cursor_on_overlay: bool = False,
                 hit: Optional[Message] = None) -> None:
        cls = app_main.ChatAssistantApp
        self.CLICK_FRESH_S = cls.CLICK_FRESH_S
        self.PENDING_CLICK_VISIBLE_S = cls.PENDING_CLICK_VISIBLE_S
        self.PENDING_CLICK_HIDDEN_S = cls.PENDING_CLICK_HIDDEN_S
        self.cfg = app_config.Config()
        self.capture = StubCapture(paused)
        self.panel = StubPanel(visible)
        self.messages = [object()] if messages is None else messages
        self._messages_ts = time.time() - age
        self._pending_click = None
        self._last_activity_ts = 0.0
        self.window_rect = (0, 0, 100, 100)
        self.target = None
        self._cursor_in_qq = cursor_in_qq
        self._cursor_on_overlay = cursor_on_overlay
        self._hit = hit
        self.empty_calls: List[tuple] = []
        self.render_calls = 0

    # ---- 被被测方法调用的接口 ----
    def _point_in_overlays(self, x: int, y: int) -> bool:
        return self._cursor_on_overlay

    def _point_in_qq_window(self, x: int, y: int) -> bool:
        return self._cursor_in_qq

    def _finish_empty_click(self, x: int, y: int) -> None:
        self.empty_calls.append((x, y))

    def _hovered_message(self, padding: int = 0, wide: bool = False) -> Optional[Message]:
        return self._hit

    def _original_message(self, msg_key: str,
                          near_bbox=None) -> Optional[Message]:      # 第 80 轮：多了坐标参数
        return None

    def _render_target(self) -> None:
        self.render_calls += 1

    def _log_text(self, text: str, limit: int = 24) -> str:
        return text[:limit]


def _with_cursor(pos=(50, 50)):
    """临时把 `w32.get_cursor_pos` 换成固定坐标。"""
    original = app_main.w32.get_cursor_pos
    app_main.w32.get_cursor_pos = lambda: pos
    return original


def case_fresh_visible_collapses_now() -> bool:
    """A：面板开着 + 坐标够新 → 立刻收起（不挂起、不申请重读）。"""
    stub = StubApp(visible=True, age=0.1)
    app_main.ChatAssistantApp._on_click_miss(stub)
    ok = (stub.empty_calls == [(50, 50)] and stub._pending_click is None
          and stub.capture.now_calls == 0)
    print(f"A 面板开着+坐标新鲜 → 立刻收起：{'对' if ok else '错'}"
          f"（收起={len(stub.empty_calls)} 挂起={stub._pending_click} 加急读={stub.capture.now_calls}）")
    return ok


def case_stale_visible_suspends_short() -> bool:
    """B：面板开着 + 坐标过期 → 挂起，但窗口只有 0.45s。"""
    stub = StubApp(visible=True, age=3.0)
    app_main.ChatAssistantApp._on_click_miss(stub)
    pending = stub._pending_click or {}
    ok = (not stub.empty_calls and pending.get("window") == 0.45
          and stub.capture.now_calls == 1)
    print(f"B 面板开着+坐标过期 → 挂起 ≤0.45s：{'对' if ok else '错'}"
          f"（窗口={pending.get('window')} 加急读={stub.capture.now_calls}）")
    return ok


def case_stale_hidden_suspends_long() -> bool:
    """C：面板没开着 + 坐标过期 → 挂起 1.2s（保证"点气泡能开出来"）。"""
    stub = StubApp(visible=False, age=3.0)
    app_main.ChatAssistantApp._on_click_miss(stub)
    pending = stub._pending_click or {}
    ok = (not stub.empty_calls and pending.get("window") == 1.2)
    print(f"C 面板关着+坐标过期 → 挂起 ≤1.2s：{'对' if ok else '错'}"
          f"（窗口={pending.get('window')}）")
    return ok


def case_second_click_collapses() -> bool:
    """D：已经有挂起的点击时再点一下 → 立刻收起（用户"再点一下就收了"的路径）。"""
    stub = StubApp(visible=True, age=3.0)
    app_main.ChatAssistantApp._on_click_miss(stub)
    first_pending = stub._pending_click is not None
    app_main.ChatAssistantApp._on_click_miss(stub)          # 同一次"没反应 → 再点一下"
    ok = first_pending and stub.empty_calls == [(50, 50)] and stub._pending_click is None
    print(f"D 再点一下就收起：{'对' if ok else '错'}"
          f"（首次挂起={first_pending} 收起={len(stub.empty_calls)}）")
    return ok


def case_overlay_click_ignored() -> bool:
    """E：点在我们自己的面板/选项条上 → 什么都不做（既不收起也不挂起）。"""
    stub = StubApp(visible=True, age=3.0, cursor_on_overlay=True)
    app_main.ChatAssistantApp._on_click_miss(stub)
    ok = not stub.empty_calls and stub._pending_click is None
    print(f"E 点面板本身 → 不动：{'对' if ok else '错'}"
          f"（收起={len(stub.empty_calls)} 挂起={stub._pending_click}）")
    return ok


def case_retry_paths() -> bool:
    """F：挂起之后 —— 超时收起 / 还没命中继续等 / 命中就去分析。"""
    # F1：超时 → 收起
    stub = StubApp(visible=True, age=1.0)
    stub._pending_click = {"t": time.time() - 0.9, "x": 11, "y": 22, "window": 0.45}
    app_main.ChatAssistantApp._retry_pending_click(stub)
    ok_timeout = stub.empty_calls == [(11, 22)] and stub._pending_click is None
    # F2：没超时且没命中 → 继续等（挂起记录保留）
    stub2 = StubApp(visible=True, age=1.0)
    stub2._pending_click = {"t": time.time(), "x": 11, "y": 22, "window": 0.45}
    app_main.ChatAssistantApp._retry_pending_click(stub2)
    ok_wait = (not stub2.empty_calls and stub2._pending_click is not None)
    # F3：命中别的消息 → 开始分析
    hit = Message(msg_key="k1", text="对方发的话", side="left", bbox=(0, 0, 10, 10))
    stub3 = StubApp(visible=False, age=1.0, hit=hit)
    stub3.target = None
    stub3._pending_click = {"t": time.time(), "x": 11, "y": 22, "window": 1.2}
    app_main.ChatAssistantApp._retry_pending_click(stub3)
    ok_hit = (stub3.render_calls == 1 and stub3.target is hit
              and stub3._pending_click is None and not stub3.empty_calls)
    ok = ok_timeout and ok_wait and ok_hit
    print(f"F 挂起后的三条出口：{'对' if ok else '错'}"
          f"（超时收起={ok_timeout} 继续等={ok_wait} 命中分析={ok_hit}）")
    return ok


def main() -> int:
    original = _with_cursor()
    try:
        results = [case_fresh_visible_collapses_now(), case_stale_visible_suspends_short(),
                   case_stale_hidden_suspends_long(), case_second_click_collapses(),
                   case_overlay_click_ignored(), case_retry_paths()]
    finally:
        app_main.w32.get_cursor_pos = original
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
