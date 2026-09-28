#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把"某条消息实际会看到的上下文"完整打印出来（用户疑问：到底包含哪些消息）。

默认用 cos 道具清单那串样本；加 --live 则读当前 QQ 窗口的可视消息，
对每条对方消息各 dump 一次上下文（不调用模型，只拼 prompt，秒级、零成本）。
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
from core.message_reader import Message, MessageReader  # noqa: E402

CHAIN = ["哦对的就这种", "痛衣内搭外边格子衬衫然后牛仔裤", "这几个我都有",
         "还差一个眼镜，头巾", "背包再塞点周边海报", "手上最好来个袋子"]


def samples_from_chain(cfg) -> list:
    messages = [Message(msg_key=f"m{i}", text=t, side="left", bbox=(0, i * 60, 900, i * 60 + 40))
                for i, t in enumerate(CHAIN)]
    return messages, [messages[3]]


def samples_live(cfg) -> list:
    reader = MessageReader(cfg)
    targets = []
    for _ in range(10):
        messages, _snapshot = reader.read()
        if messages:
            break
        time.sleep(1.5)
    # 多读几轮，让"累积历史"长起来（真实运行时也是这样持续的）
    for _ in range(5):
        messages, _snapshot = reader.read()
        time.sleep(0.6)
    targets = [m for m in messages if m.side == "left" and not m.is_image]
    print(f"（本轮读到 {len(messages)} 条可见；累积历史 {len(reader._history)} 条）")
    return reader, targets


def main() -> int:
    cfg = app_config.load_config()
    analyzer = Analyzer(cfg)
    live = "--live" in sys.argv
    if live:
        reader, targets = samples_live(cfg)
        messages = None
    else:
        messages, targets = samples_from_chain(cfg)
    print(f"配置：context_messages={cfg.analyzer.context_messages}｜"
          f"context_token_budget={cfg.analyzer.context_token_budget}（≈{int(cfg.analyzer.context_token_budget*1.5)} 字符）"
          f"｜context_following={cfg.analyzer.context_following}｜"
          f"history_max={cfg.analyzer.context_history_max}｜"
          f"summary_chars={cfg.analyzer.summary_chars}")
    if not targets:
        print("没有可分析的消息")
        return 1
    for target in targets[:3]:
        # --live 时用**真实的** build_context（含累积历史/后文/预算），保证 dump 就是实况
        context = reader.build_context(reader._history + [], target) if live else \
            analyzer_message_context(messages, target, cfg)
        prompt = analyzer.build_messages(context, target)
        user_part = prompt[1]["content"]
        print(f"\n===== 目标：{target.text} =====")
        print(f"上下文条数 {len(context)}（其中目标 1 条、后文 "
              f"{len([m for m in context if m.side == 'after'])} 条）")
        print(user_part)
        print(f"—— prompt 字符数 {sum(len(m['content']) for m in prompt)}"
              f"（≈{int(sum(len(m['content']) for m in prompt)/1.5)} token）")
    return 0


def analyzer_message_context(messages, target, cfg) -> list:
    """复刻 MessageReader.build_context 的裁剪逻辑（不依赖 UIA）。"""
    limit = cfg.analyzer.context_messages
    upto = [m for m in messages if m.bbox[1] <= target.bbox[3] and not m.is_image]
    visible = upto[-limit:]
    following = int(getattr(cfg.analyzer, "context_following", 0) or 0)
    after = [m for m in messages if m.bbox[1] > target.bbox[3] and not m.is_image][:following]
    after = [Message(msg_key=m.msg_key + "a", text=m.text, side="after", bbox=m.bbox)
             for m in after]
    return list(visible) + after


if __name__ == "__main__":
    raise SystemExit(main())
