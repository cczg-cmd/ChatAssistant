#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑一条真实样本，分别用两套 prompt 配置出结果（CPU 后端，秒级）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as app_config  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402
from tools.ab_context import _pool_and_visible, _target, assemble  # noqa: E402

FIXTURE = ROOT / "tmp" / "ctx_fixture.json"


def main() -> int:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    target_msg = next(m for m in data["pool"] if m["side"] == "left" and not m.get("is_image"))
    scn = {"name": "live", "messages": data["visible"], "target": target_msg["key"],
           "pool": data["pool"], "virtual_hist": data.get("virtual_hist") or []}
    pool, _v = _pool_and_visible(scn)
    target = _target(scn, pool)
    cfg = app_config.load_config()
    analyzer = Analyzer(cfg)
    analyzer.cfg.analyzer.n_gpu_layers = 0
    if not analyzer.load(app_config.resolve_model_path(cfg)):
        print("模型加载失败")
        return 1
    context = assemble(scn, "v1_order", cfg.analyzer.context_messages, cfg.analyzer.context_token_budget)
    for profile in ("默认配置", "猫娘配置"):
        app_config.apply_profile(cfg, profile)
        cfg.active_profile = profile
        analyzer.extra_seed = 0
        quick = analyzer.analyze_quick(target, context, use_cache=False)
        replies = analyzer.analyze_replies(target, context, quick=quick)
        print(f"\n===== {profile}｜目标：{target.text} =====")
        if quick:
            print(f"  意图：{quick.intent}｜情绪：{quick.emotion}｜危险度：{quick.danger_level}")
            print(f"  建议：{quick.suggestion}")
        for item in (replies.replies if replies else []):
            print(f"  [{item.style}] {item.text}")
    analyzer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
