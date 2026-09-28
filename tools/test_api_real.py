#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用**真实的分析 prompt** 打一次 DeepSeek，看返回是否完整/是否被截断。

目的：面板只显示"—"时，区分"max_tokens 太小导致 JSON 截断"和"模型没按 JSON 输出"。
会消耗几百 token（算在当日预算里，脚本会打印实际用量）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402
from core.message_reader import Message  # noqa: E402


def main() -> int:
    cfg = app_config.load_config()
    cfg.analyzer.backend = "api"
    analyzer = Analyzer(cfg)
    target = Message(msg_key="t", text="你电脑有去清过灰嘛", side="left", bbox=(0, 0, 300, 40))
    context = [Message(msg_key="b1", text="我现在打开游戏就会掉网", side="left", bbox=(0, 0, 0, 0)),
               target]
    messages = analyzer.build_messages(context, target)
    prompt_chars = sum(len(m["content"]) for m in messages)
    print(f"prompt 字符数 {prompt_chars}（≈{int(prompt_chars / 1.5)} token）"
          f"｜当前 api.max_tokens_replies={cfg.api.max_tokens_replies}")
    for limit in (cfg.api.max_tokens_replies, 420):
        raw = analyzer._api_generate(messages, max_tokens=limit)
        print(f"\n--- max_tokens={limit} ---")
        if raw is None:
            print("  返回 None（预算/冷却/报错，见日志）")
            continue
        print(f"  原始返回 {len(raw)} 字符：{raw[:260]!r}")
        try:
            payload = json.loads(raw)
            print(f"  JSON 解析 ✓ 字段：{list(payload.keys())}")
            options = payload.get("reply_options") or []
            print(f"  选项 {len(options)} 条：" + "；".join(
                f"[{o.get('style')}]{o.get('text')}" for o in options))
        except Exception as exc:
            print(f"  JSON 解析 ✗ {type(exc).__name__}: {exc}")
    print("\n今日累计用量:", analyzer.today_usage())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
