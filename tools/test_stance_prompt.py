#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 75 轮回归：立场规则（认同/反调/吐槽）在全局与字段说明里都到位、喵的要求仍在。

用法：python tools/test_stance_prompt.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as c  # noqa: E402


def main() -> int:
    cfg = c.load_config()
    results = []

    style = c.DEFAULT_STYLE_PROMPT
    has_hard = "【硬要求】三条回复选项的立场必须拉开" in style
    has_tsukkomi = "一条吐槽" in style and "一条认同" in style and "一条反调" in style
    print(f"① 全局分析风格里有立场硬要求：{has_hard}｜含 认同/反调/吐槽：{has_tsukkomi}")
    results += [has_hard, has_tsukkomi]

    for name in ("默认配置", "猫娘配置"):
        c.apply_profile(cfg, name)
        prompt = c.field_prompt(cfg, "option")
        ok = all(k in prompt for k in ("认同", "反调", "吐槽"))
        no_neutral = "中立" not in prompt
        print(f"② {name}：三立场齐={ok}｜已无“中立”={no_neutral}｜含喵={'喵' in prompt}")
        results += [ok, no_neutral]

    # ③ 真正发给模型的那段"全局分析风格"里必须有立场硬要求
    #    （它不在 build_system_prompt 里，而是 build_messages 单独拼在系统消息末尾；
    #     而且**存在 config.json**，所以改默认值后必须同步 JSON —— 这里就是在守这条）
    effective = (cfg.analysis_style_prompt or "").strip()
    ok_eff = "【硬要求】三条回复选项的立场必须拉开" in effective
    same_as_code = effective == c.DEFAULT_STYLE_PROMPT.strip()
    print(f"③ 运行时风格提示含立场硬要求：{ok_eff}｜与代码默认一致：{same_as_code}"
          f"（{len(effective)} 字）")
    results += [ok_eff, same_as_code]

    print(f"\n{sum(results)}/{len(results)} 通过")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
