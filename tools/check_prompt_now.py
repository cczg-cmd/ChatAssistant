#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零成本检查：两套内置配置的实际系统提示词是否含本轮新规则。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as c  # noqa: E402


def main() -> int:
    cfg = c.load_config()
    for name in ("默认配置", "猫娘配置"):
        c.apply_profile(cfg, name)
        prompt = c.build_system_prompt(cfg)
        print(f"===== {name} ｜ 系统提示词 {len(prompt)} 字")
        print("  含[言外之意]:", "言外之意" in prompt)
        print("  含[发散不等于编造]:", "发散不等于编造" in prompt)
        print("  含[别给最常规](建议/选项):", "别给最常规" in prompt and "不要给最常规" in prompt)
        print("  含[问的事你不知道答案时别编]:", "你不知道答案时" in prompt)
    entry = c.find_profile(cfg, "猫娘配置") or {}
    print("内存里的猫娘配置已被代码最新版覆盖:",
          "不要给最常规" in entry.get("option_prompt", ""))
    # 分段长度（对照 tmp/measure_split_out.txt 里的旧值：HEAD 568 / 意图 86 / 情绪 44 /
    # 危险度 179 / 建议 121 / 选项 211 / TAIL 72 / 合计 1555）
    parts = [("HEAD", c.SYSTEM_PROMPT_HEAD)] + [
        (key, c.DEFAULT_FIELD_PROMPTS[key])
        for key in ("intent", "emotion", "danger", "suggestion", "option")
    ] + [("TAIL", c.SYSTEM_PROMPT_TAIL)]
    print("--- 默认配置分段字数 ---")
    for name, text in parts:
        print(f"  {name}: {len(text)} 字")
    print(f"  合计: {sum(len(t) for _n, t in parts)} 字")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
