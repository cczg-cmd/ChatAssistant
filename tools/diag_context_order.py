#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断"喂给模型的上下文"的顺序与完整性（只读、不调模型）。

对当前累积池里的每条"对方"消息当一次目标，检查：
  ① 【背景/紧邻的上文】里有没有**比目标更晚**的消息（后文冒充前文）；
  ② "紧邻的上文"是不是池子里真正的紧邻前两条；
  ③ 有没有比目标更晚的池内消息**完全没进** prompt（后文被吞掉）；
  ④ prompt 规模（字符/token 估算）与是否触发预算裁剪。

用法：<py311> tmp/diag_context_order.py [--reads 6]
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


def collect(cfg, reads: int):
    reader = MessageReader(cfg)
    messages, snapshot = [], {}
    for _ in range(10):
        messages, snapshot = reader.read()
        if messages:
            break
        time.sleep(1.5)
    for _ in range(reads):                      # 多读几轮，让池子像真实运行时那样长起来
        messages, snapshot = reader.read()
        time.sleep(0.5)
    return reader, messages, snapshot


def main() -> int:
    reads = 6
    if "--reads" in sys.argv:
        reads = int(sys.argv[sys.argv.index("--reads") + 1])
    cfg = app_config.load_config()
    analyzer = Analyzer(cfg)
    reader, messages, snapshot = collect(cfg, reads)
    pool = list(reader._history)
    print(f"可见 {len(messages)} 条｜池子 {len(pool)} 条｜"
          f"预算 {cfg.analyzer.context_token_budget} token / {cfg.analyzer.context_messages} 条"
          f"｜后文配置 {cfg.analyzer.context_following}")
    print("\n池子顺序（下标 → 说话人 文本）：")
    for index, msg in enumerate(pool):
        who = "[图]" if msg.is_image else ("对方" if msg.side == "left" else "我  ")
        print(f"  {index:>3}  seg={msg.segment}  {who}  {msg.text[:34]}")
    index_of = {}
    for index, msg in enumerate(pool):          # 同一文本可能重复，取**最后一个**（较新）
        index_of[msg.msg_key] = index
    targets = [m for m in pool if m.side == "left" and not m.is_image]
    print(f"\n共 {len(targets)} 条对方消息可当目标")
    violations = 0
    for target in targets:
        tidx = index_of.get(target.msg_key)
        context = reader.build_context(messages, target)
        prompt = analyzer.build_messages(context, target)
        user_part = prompt[1]["content"]
        later_in_before, unknown_pos, after_side = [], [], []
        for msg in context:
            if msg.msg_key == target.msg_key:
                continue
            if msg.side == "after":
                after_side.append(msg.text[:16])
                continue
            if msg.side == "history":           # 虚拟化历史：没有下标，位置只能靠猜
                unknown_pos.append(msg.text[:16])
                continue
            midx = index_of.get(msg.msg_key)
            if midx is None:
                unknown_pos.append(msg.text[:16])
            elif tidx is not None and midx > tidx:
                later_in_before.append((midx, msg.text[:16]))
        missing_after = [m.text[:16] for m in pool[tidx + 1:] if not m.is_image] if tidx is not None else []
        flag = "⚠" if later_in_before else " "
        if later_in_before:
            violations += 1
        chars = sum(len(m["content"]) for m in prompt)
        print(f"\n{flag} 目标[{tidx}]：{target.text[:30]}")
        print(f"    上下文 {len(context)} 条｜prompt {chars} 字符（≈{int(chars / 1.5)} token）")
        print(f"    前文里混入的**更晚**消息：{later_in_before if later_in_before else '无'}")
        print(f"    池内更晚但没进 prompt：{missing_after[:4] if missing_after else '无'}")
        print(f"    位置未知（虚拟化/拿不到下标）：{unknown_pos[:4] if unknown_pos else '无'}")
        print("    —— 实际 prompt 正文 ——")
        for line in user_part.splitlines():
            print("      " + line)
    print(f"\n结论：{len(targets)} 条目标里有 {violations} 条的**前文里混入了更晚的消息**")
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
