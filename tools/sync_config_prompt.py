#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 config.json 里的 system_prompt 同步成 config.py 的当前模板。

背景：core/analyzer.build_messages 用的是模块常量 SYSTEM_PROMPT_TEMPLATE，
config.system_prompt 只是历史遗留字段（运行时没人读）。但 config.json 里存的
还是很早以前的弱化版，容易让人误以为"改 config.py 没用"。这里对齐，
同时打印改前改后的关键字段，确认没有动到别的配置。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402

KEYS = ["system_prompt", "analysis_style_prompt", "field_limits", "danger_anchors"]


def main() -> int:
    path = ROOT / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    before = {k: data.get(k) for k in KEYS}
    data["system_prompt"] = app_config.SYSTEM_PROMPT_TEMPLATE
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    after = json.loads(path.read_text(encoding="utf-8"))

    print(f"system_prompt：{len(before['system_prompt'] or '')} 字 → "
          f"{len(after['system_prompt'])} 字")
    for key in KEYS[1:]:
        same = json.dumps(before[key], ensure_ascii=False, sort_keys=True) == \
            json.dumps(after.get(key), ensure_ascii=False, sort_keys=True)
        print(f"  {key}: {'未变' if same else '变了(!)'}")
    print("ui.min_message_chars =", after["ui"]["min_message_chars"])
    print("analyzer 字段数 =", len(after["analyzer"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
