#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证"配置列表"：内置两套、切到猫娘配置后提示词与字段名的实际变化、
自定义配置的增删、以及 GBNF 键名不受影响。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


def main() -> int:
    cfg = config.load_config()
    print("配置列表:", [(e["name"], "内置" if e.get("builtin") else "自建")
                        for e in cfg.fields_profiles])
    print("生效配置:", cfg.active_profile)

    print("\n--- 切到「猫娘配置」---")
    assert config.apply_profile(cfg, config.CATGIRL_PROFILE_NAME)
    prompt = config.build_system_prompt(cfg)
    for line in prompt.splitlines():
        if any(k in line for k in ("意图", "情绪", "危险度", "回复建议", "回复选项")):
            print("   ", line[:120])

    print("\n--- 新建一套自定义配置并切换 ---")
    entry = config.profile_entry("女仆配置")
    entry["option_prompt"] = "三条回复都用女仆口吻，称呼对方为「主人」"
    entry["suggestion_prompt"] = "给主人一句不超过 15 字的提示"
    cfg.fields_profiles.append(entry)
    assert config.apply_profile(cfg, "女仆配置")
    prompt2 = config.build_system_prompt(cfg)
    print("   ", [l[:60] for l in prompt2.splitlines() if "女仆" in l or "主人" in l])
    cfg.fields_profiles = [e for e in cfg.fields_profiles if e["name"] != "女仆配置"]
    print("   删除自建配置后剩:", [e["name"] for e in cfg.fields_profiles])

    print("\n--- 切回默认配置 ---")
    assert config.apply_profile(cfg, config.DEFAULT_PROFILE_NAME)
    print("   字段名:", {k: config.field_name(cfg, k) for k in config.FIELD_KEYS})
    print("   封顶硬规则:", cfg.fields.danger_cap_rule, "（已关，规则写进 prompt）")
    print("   默认 danger prompt 含封顶说明:",
          "最高只能给 4" in config.field_prompt(cfg, "danger"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
