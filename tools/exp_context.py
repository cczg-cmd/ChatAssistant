#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/exp_context.py - 归因实验：分析不契合语境，是上下文 / prompt / 模型哪一环的问题？

控制变量法，对同一条真实消息跑 6 种组合：
  上下文三档：完整（历史+可见） / 仅可见 / 仅目标消息
  prompt 两档：当前完整 prompt / 极简 prompt（只要求字段，不讲 danger 细则）
模型固定（默认 7B），另跑一次 3B 作对照。
输出每条组合的 intent / emotion / danger / suggestion，供人工判断"贴不贴语境"。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

WORKSPACE = Path(r"D:\QQChatAssistant")
sys.path.insert(0, str(WORKSPACE))

from config import load_config, Config            # noqa: E402
from core.analyzer import Analyzer                # noqa: E402
from core.message_reader import MessageReader     # noqa: E402
from utils import win32_api as w32                # noqa: E402

MINIMAL_PROMPT = (
    "你是QQ聊天分析助手。读最后一条“对方”的消息，结合上面上下文，输出 JSON：\n"
    "intent（≤14字，对方在说什么/想干什么）、emotion（≤10字）、danger_level（0-10）、"
    "suggestion（≤40字，针对这条消息具体说明该怎么做）、confidence（0-1）。\n"
    "所有字段纯文本，只输出 JSON。"
)


def main() -> int:
    w32.set_dpi_awareness()
    base = load_config()
    reader = MessageReader(base)
    messages, snap = reader.read()
    lefts = [m for m in messages if m.is_other_party and not m.is_image]
    if not lefts:
        print("当前会话没有对方消息可测")
        return 1
    target = lefts[-1]
    ctx_full = reader.build_context(messages, target)
    ctx_visible = [m for m in messages
                   if m.bbox[1] <= target.bbox[3] and not m.is_image] or [target]
    ctx_none = [target]
    print(f"目标消息：{target.text}")
    print(f"上下文：完整 {len(ctx_full)} 条 / 仅可见 {len(ctx_visible)} 条 / 仅目标 1 条")

    models = [("7B", base.paths.model), ("3B", str(WORKSPACE / 'models' / 'qwen2.5-3b-instruct-q4_k_m.gguf'))]
    for model_name, model_path in models:
        print("=" * 78)
        print(f"### 模型 {model_name}")
        for prompt_name, prompt in (("当前prompt", base.system_prompt), ("极简prompt", MINIMAL_PROMPT)):
            cfg = load_config()
            cfg.system_prompt = prompt
            a = Analyzer(cfg)
            if not a.load(model_path):
                print("  加载失败:", a.last_error)
                continue
            for ctx_name, ctx in (("历史+可见", ctx_full), ("仅可见", ctx_visible), ("仅目标", ctx_none)):
                t0 = time.perf_counter()
                q = a.analyze_quick(target, ctx, use_cache=False)
                dt = (time.perf_counter() - t0) * 1000
                if q is None:
                    print(f"  [{prompt_name}][{ctx_name}] 生成失败")
                    continue
                print(f"  [{prompt_name}][{ctx_name}] {dt:5.0f}ms → "
                      f"intent={q.intent} | emotion={q.emotion} | danger={q.danger_level} | "
                      f"建议={q.suggestion}")
            a.close()
    reader.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
