#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证字段自定义：改名 + 每个字段独立 prompt 是否真的进到系统提示词里，
并且 JSON 结构/长度上限仍由程序固定。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


def show(title: str, cfg) -> None:
    prompt = config.build_system_prompt(cfg)
    print(f"\n=== {title}（{len(prompt)} 字）===")
    for line in prompt.splitlines():
        if any(key in line for key in ("意图", "情绪", "危险度", "兴趣水平", "回复建议",
                                       "回复选项", "女仆")):
            print("   ", line[:110])


def main() -> int:
    cfg = config.load_config()
    print("默认字段名:", {k: config.field_name(cfg, k)
                          for k in ("intent", "emotion", "danger", "suggestion", "option")})
    show("默认", cfg)

    # 场景 A：danger → 兴趣水平
    cfg.fields.danger_name = "兴趣水平"
    cfg.fields.danger_prompt = ("0-10 整数，越高表示对方越有兴趣、越想继续聊；"
                                "5 以上要指出他具体对什么感兴趣")
    # 场景 B：回复选项 → 女仆风格
    cfg.fields.option_prompt = ("三条回复都用女仆口吻，称呼对方为「主人」，"
                                "语气恭敬、可爱、简短")
    # 场景 C：回复建议也自定义
    cfg.fields.suggestion_prompt = "给主人一句极短的应对提示，不超过 15 字"
    show("自定义后", cfg)

    # 语法仍固定：字段键名与长度上限不受影响
    from core.analyzer import build_grammar, build_quick_grammar
    grammar = build_quick_grammar(cfg)
    # 注意：GBNF 文本里键名是转义写法（`\"intent\"`），所以不能用 `'"intent"'` 去比 ——
    # 旧写法一直打印 False（假红），第 80 轮续顺手改对。
    print("\n语法里仍是固定键名:",
          all(f'\\"{key}\\"' in grammar for key in ('intent', 'emotion',
                                                   'danger_level', 'suggestion')))
    print("intent 上限仍为:", cfg.field_limits["intent"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
