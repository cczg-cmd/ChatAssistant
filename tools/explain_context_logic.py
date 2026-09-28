#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对照实验（不用 QQ、不调模型）：同一个池子里，目标在屏幕"靠下"和"靠上"时，
有多少条上下文来自池子、多少条来自屏幕。

用法：python tmp/explain_context_logic.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from core.message_reader import MessageReader  # noqa: E402
from tools.test_pool_merge import FakeUia, FakeWindow, make_reader  # noqa: E402
from core import message_reader as mr  # noqa: E402


def run() -> None:
    cfg = app_config.load_config()
    limit = int(cfg.analyzer.context_messages)
    # 30 条对话；先喂 3 个窗口让池子攒到 22 条
    conv = []
    for i in range(30):
        conv.append(f"对方{i:02d}" if i % 2 == 0 else f"我{i:02d}")
    reader, uia = make_reader()
    # 两次窗口之间重叠 3 条 → 池子会一路累积（模拟用户缓慢往下看）
    uia.script = [conv[0:8], conv[5:13], conv[10:18], conv[13:21], conv[15:23]]
    while uia.script:
        reader.read()
    pool_before = len(reader._history)

    # 现在"屏幕上"只显示 4 条：目标上方可见的条目远少于预算，差额必须从池子补
    visible_slice = conv[19:23]
    uia.script = [visible_slice]
    messages, _snap = reader.read()
    left = [m for m in messages if m.side == "left"]
    print(f"池子 {len(reader._history)} 条（读前 {pool_before}）｜屏幕可见 {len(messages)} 条")
    for label, target in (("目标靠下（屏幕第 5 条）", left[2]), ("目标靠上（屏幕第 1 条）", left[0])):
        context = reader.build_context(messages, target)
        screen_keys = {m.msg_key for m in messages}
        from_pool = [m for m in context if m.msg_key not in screen_keys
                     and m.side not in ("history", "after")]
        print(f"\n{label}：{target.text}")
        print(f"  进 prompt 共 {len(context)} 条｜来自池子（屏幕上没有的） {len(from_pool)} 条"
              f"｜预算 {limit} 条")
        print("  顺序：" + " → ".join(m.text for m in context))


def main() -> int:
    cfg_backup = app_config.load_config
    assert cfg_backup
    mr.w32.find_qq_chat_window = lambda *a, **k: FakeWindow()
    assert FakeUia and MessageReader
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
