#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 79 轮回归：同一次物理点击只处理一次（轮询 + Raw Input 双通道去重）。

真实事故（用户："点击选项/气泡，偶尔刚要展开马上又收起，多点几下才好"）：日志里
同一毫秒级窗口内既出现"点击消息 → 开始分析"又出现"点击同一条 → 收起浮窗" ——
轮询（30ms 轮 GetAsyncKeyState）先处理了这一击，Raw Input 6ms 后到达又处理了一遍，
第二次看到"同一条消息且面板可见"就当成"再点一次收起"。旧去重只做了单向，所以挡不住。

用法：python tools/test_click_dedup.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main as app_main  # noqa: E402
from config import Config  # noqa: E402


class Stub:
    def __init__(self) -> None:
        self.cfg = Config()
        self.cfg.ui.trigger_mode = "click"
        self._last_click_handled = 0.0
        self.toggles = 0

    def _toggle_target(self, padding: int) -> None:
        self.toggles += 1

    # 让桩对象也走真实的去重实现（避免测试复制一份逻辑，改一处漏一处）
    _click_recently_handled = app_main.ChatAssistantApp._click_recently_handled


def case_single_click_once() -> bool:
    """同一击的两条通道：Raw Input 连续两次（模拟重复投递）只算一次。"""
    stub = Stub()
    app_main.ChatAssistantApp.on_raw_click(stub, 100, 100)
    app_main.ChatAssistantApp.on_raw_click(stub, 100, 100)
    ok = stub.toggles == 1
    print(f"A Raw Input 重复投递同一击 → 处理 {stub.toggles} 次（期望 1）")
    return ok


def case_poll_then_raw() -> bool:
    """复现真事故：轮询先处理 → Raw Input 紧接着到达必须被挡掉。"""
    stub = Stub()
    # 模拟 _poll_click 已经在同一击上登记并处理（它做的正是这两步）
    if app_main.ChatAssistantApp._click_recently_handled(stub):
        return False
    stub._last_click_handled = time.time()
    app_main.ChatAssistantApp.on_raw_click(stub, 100, 100)     # 6ms 后到达的那一次
    ok = stub.toggles == 0
    print(f"B 轮询先处理后 Raw Input 再来 → 额外处理 {stub.toggles} 次（期望 0，不会被当成再点收起）")
    return ok


def case_new_click_after_interval() -> bool:
    """隔了足够久（>0.3s）的第二次点击必须照常处理（否则"点两下"就失灵）。"""
    stub = Stub()
    app_main.ChatAssistantApp.on_raw_click(stub, 100, 100)
    stub._last_click_handled = time.time() - 0.5
    app_main.ChatAssistantApp.on_raw_click(stub, 100, 100)
    ok = stub.toggles == 2
    print(f"C 间隔 0.5s 的两次点击 → 处理 {stub.toggles} 次（期望 2）")
    return ok


def main() -> int:
    results = [case_single_click_once(), case_poll_then_raw(), case_new_click_after_interval()]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
