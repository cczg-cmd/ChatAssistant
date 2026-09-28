#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抽查"意图"口径：是不是"对方想干什么 + 什么心情"（而不是复述话题）。

对 3 条 cos 清单样本 + 1 条真实快照样本各跑一次第一段（CPU，不占显存）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402
from tools.ab_context import _pool_and_visible, _target, assemble, builtin_scenarios  # noqa: E402

FIXTURE = ROOT / "tmp" / "ctx_fixture.json"


def main() -> int:
    cfg = app_config.load_config()
    scenarios = builtin_scenarios()
    if FIXTURE.exists():
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for i, target in enumerate([m for m in data["pool"]
                                    if m["side"] == "left" and not m.get("is_image")], 1):
            scenarios.append({"name": f"live-{i}", "messages": data["visible"],
                              "target": target["key"], "pool": data["pool"],
                              "virtual_hist": data.get("virtual_hist") or []})
    analyzer = Analyzer(cfg)
    analyzer.cfg.analyzer.n_gpu_layers = 0
    if not analyzer.load(app_config.resolve_model_path(cfg)):
        print("模型加载失败")
        return 1
    analyzer.extra_seed = 0
    for scn in scenarios:
        pool, _v = _pool_and_visible(scn)
        target = _target(scn, pool)
        context = assemble(scn, "v1_order", cfg.analyzer.context_messages,
                           cfg.analyzer.context_token_budget)
        quick = analyzer.analyze_quick(target, context, use_cache=False)
        if quick:
            print(f"{scn['name'][:16]:<16}｜对方说：{target.text[:18]:<20}→ "
                  f"意图：{quick.intent}｜情绪：{quick.emotion}｜危险度：{quick.danger_level}")
    analyzer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
