#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实况对账：把"上下文历史池"和"这次分析真正喂给模型的消息"逐条列出来。

用法：python tmp/explain_context.py [目标序号]
    默认目标 = 屏幕上最后一条对方消息（最常点的位置）。
输出：池子每条 → 用/不用（不用的话给出原因）；再打印真正进 prompt 的顺序。
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
from core.analyzer import Analyzer  # noqa: E402
from core.message_reader import MessageReader  # noqa: E402


def main() -> int:
    cfg = app_config.load_config()
    reader = MessageReader(cfg)
    # 像真实运行那样连续读几轮，让池子长起来
    messages = []
    for _ in range(8):
        messages, _snap = reader.read()
        if messages:
            break
        time.sleep(1.0)
    for _ in range(6):
        messages, _snap = reader.read()
        time.sleep(0.3)
    if not messages:
        print("读不到消息（QQ 窗口没开？）")
        return 1

    left = [m for m in messages if m.side == "left" and not m.is_image]
    if not left:
        print("屏幕上没有对方消息")
        return 1
    index = int(sys.argv[1]) if len(sys.argv) > 1 else len(left)
    target = left[min(max(index, 1), len(left)) - 1]

    limit = int(cfg.analyzer.context_messages)
    budget = int(cfg.analyzer.context_token_budget)
    context = reader.build_context(messages, target)
    analyzer = Analyzer(cfg)
    prompt = analyzer.build_messages(context, target)

    print(f"目标：{target.text[:40]!r}（对方，屏幕 y={target.bbox[1]}）")
    print(f"可见消息 {len(messages)} 条｜累积池 {len(reader._history)} 条｜"
          f"预算 {limit} 条 / {budget} token"
          f"（≈{int(budget * 1.5)} 字符）｜历史行 {len(getattr(reader.uia, 'history_texts', []))} 条")

    used = [m.msg_key for m in context]
    upto = [m for m in messages if m.bbox[1] <= target.bbox[3] and not m.is_image]
    visible = upto[-limit:]
    visible_keys = {m.msg_key for m in visible}
    need = max(0, limit - len(visible))
    print(f"\n屏幕上目标及其上方可见 {len(upto)} 条 → 取最近 {len(visible)} 条；"
          f"给池子留的名额 = {limit} - {len(visible)} = **{need} 条**")

    print("\n=== 累积池逐条对账 ===")
    pool_keys = [m.msg_key for m in reader._history]
    try:
        target_index = len(pool_keys) - 1 - pool_keys[::-1].index(target.msg_key)
    except ValueError:
        target_index = None
    for i, msg in enumerate(reader._history):
        speaker = "对方" if msg.side == "left" else "我"
        if msg.is_image:
            why = "图片占位（不进 prompt）"
        elif msg.msg_key == target.msg_key and i == target_index:
            why = "★ 目标本身"
        elif msg.msg_key in visible_keys:
            why = "已在屏幕上（按屏幕坐标进 prompt，不从这里取）"
        elif target_index is None:
            why = "目标不在池子里 → 兜底不用池子"
        elif i > target_index:
            why = "比目标更晚（后文口径 = 0，不进 prompt）"
        elif need == 0:
            why = "屏幕已占满名额（池子这次 0 条进 prompt）"
        elif msg.msg_key in used:
            why = "✓ 取自池子（当【紧邻的上文】/【背景】）"
        else:
            why = "被预算裁掉（更早的优先丢）"
        print(f"  {i:3d} {speaker}: {msg.text[:34]:36s} {why}")

    print("\n=== 真正进 prompt 的顺序 ===")
    for msg in context:
        speaker = {"left": "对方", "right": "我", "history": "历史", "after": "后文"}.get(
            msg.side, msg.side)
        print(f"  {speaker}: {msg.text[:40]}")
    total = sum(len(m["content"]) for m in prompt)
    print(f"\nprompt 合计 {total} 字符 ≈ {int(total / 1.45)} token")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
