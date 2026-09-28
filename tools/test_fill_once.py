#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 79 轮回归：一键填入**只发一次 Ctrl+V**（用户："点击选项会复制两次"）。

背景：新版 QQ 不把输入框内容暴露给 UIA（回读永远空/None），旧逻辑据此误判"没贴进去"
→ 补粘贴一次 → 输入框里两份文本。

做法：把 `ChatAssistantApp._fill_or_copy` 跑在桩对象上（不碰真 QQ、不注入输入），
把 `main.w32` 的鼠标/键盘/剪贴板函数换成计数器，断言：
  A. 读不到输入框内容（新版常态）→ 只贴 1 次、且按成功返回（不弹失败提示）；
  B. 回读能看到目标文本 → 只贴 1 次、且返回成功（回读校验路径）；
  C. 能读、但内容里没有目标文本 → 只贴 1 次、返回失败（如实告知，但**不补粘**）。

用法：python tools/test_fill_once.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main as app_main  # noqa: E402
from config import Config  # noqa: E402


class FakeUia:
    def __init__(self, read_value):
        self._read = read_value

    def find_input_box(self, _rect):
        return [100, 200, 400, 260]

    def focus_input_box(self, _rect):
        return False                    # 强制走"点击拿焦点"那条路

    def read_input_text(self, _rect):
        return self._read


class FakeReader:
    def __init__(self, read_value):
        self.uia = FakeUia(read_value)


class Stub:
    """只实现 _fill_or_copy 用到的那几样东西。"""

    def __init__(self, read_value, contains=False):
        self.reader = FakeReader(read_value)
        self.window_rect = [0, 0, 1000, 800]
        self.snapshot = {"window": {"hwnd": 4242}}
        self.cfg = Config()
        self._contains = contains
        self.pastes = 0
        self.clicks = 0

    # --- 被测方法依赖的两个校验入口 ---
    def _wait_input_contains(self, _text, _timeout_ms, interval=0.05):
        return False

    def _input_contains(self, _text):
        return self._contains

    def _log_text(self, text, limit=24):
        return text[:limit]


def run_case(read_value, contains, expect_ok, label):
    stub = Stub(read_value, contains)
    saved = {name: getattr(app_main.w32, name) for name in
             ("bring_window_to_front", "ensure_foreground", "click_at_physical",
              "fill_text_into_foreground", "clip_read_text", "clip_write_text")}

    def fake_paste(*_a, **_k):
        stub.pastes += 1
        return True

    app_main.w32.bring_window_to_front = lambda *_a, **_k: True
    app_main.w32.ensure_foreground = lambda *_a, **_k: True
    app_main.w32.click_at_physical = lambda *_a, **_k: (setattr(stub, "clicks", stub.clicks + 1)
                                                        or True)
    app_main.w32.fill_text_into_foreground = fake_paste
    app_main.w32.clip_read_text = lambda *_a, **_k: ""
    app_main.w32.clip_write_text = lambda *_a, **_k: True
    try:
        ok, note = app_main.ChatAssistantApp._fill_or_copy(stub, "测试文本", "paste")
    finally:
        for name, fn in saved.items():
            setattr(app_main.w32, name, fn)
    good = (ok is expect_ok) and stub.pastes == 1
    print(f"{label}：{'对' if good else '错'}（贴 {stub.pastes} 次、点击 {stub.clicks} 次、"
          f"ok={ok}、note={note[:28]!r}）")
    return good


def main() -> int:
    results = [
        # A. 读不到内容（新版 QQ 常态）→ 贴 1 次、按成功返回
        run_case(None, False, True, "A 读不到输入框内容"),
        # B. 能读到目标文本 → 贴 1 次、成功
        run_case("测试文本已填入", True, True, "B 回读已确认"),
        # C. 能读、但内容不对 → 贴 1 次、如实报失败
        run_case("别的内容", False, False, "C 能读但没有目标文本"),
    ]
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
