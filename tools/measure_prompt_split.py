#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量一次"输入预算到底花在哪"：system（指令）vs 上下文（真实对话），用真实 tokenizer。

输出：各段字符数 / token 数，以及 v0/v1/v2 三种上下文策略下的对照。
CPU 后端（不抢显存），只做 tokenize，不生成。
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
from tools.ab_context import _pool_and_visible, _target, assemble  # noqa: E402

FIXTURE = ROOT / "tmp" / "ctx_fixture.json"


def main() -> int:
    cfg = app_config.load_config()
    if "--noread" not in sys.argv and "--file" not in sys.argv:
        pass
    data = None
    if "--file" in sys.argv:
        data = json.loads(Path(sys.argv[sys.argv.index("--file") + 1]).read_text(encoding="utf-8"))
    elif FIXTURE.exists():
        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    scenarios = []
    if data:
        for i, target in enumerate([m for m in data["pool"]
                                    if m["side"] == "left" and not m.get("is_image")], 1):
            scenarios.append({"name": f"live-{i}-{target['text'][:12]}", "messages": data["visible"],
                              "target": target["key"], "pool": data["pool"],
                              "virtual_hist": data.get("virtual_hist") or []})
    from tools.ab_context import builtin_scenarios
    scenarios += builtin_scenarios()

    analyzer = Analyzer(cfg)
    analyzer.cfg.analyzer.n_gpu_layers = 0
    if not analyzer.load(app_config.resolve_model_path(cfg)):
        print("模型加载失败")
        return 1
    llm = analyzer.llm
    counts = llm.tokenize(cfg.system_prompt.encode("utf-8")) if False else None  # noqa: F841

    def tok(text: str) -> int:
        return len(llm.tokenize(text.encode("utf-8")))

    full = app_config.build_system_prompt(cfg, compact=False)
    compact = app_config.build_system_prompt(cfg, compact=True)
    marker = compact.find("JSON 的**键名必须严格用下面的英文")
    slim = compact[:marker].rstrip() if marker > 0 else compact
    style = (cfg.analysis_style_prompt or "").strip() or app_config.DEFAULT_STYLE_PROMPT
    head = app_config.SYSTEM_PROMPT_HEAD
    tail = app_config.SYSTEM_PROMPT_TAIL
    print("=== 系统提示（指令）parts ===")
    print(f"  HEAD            {len(head):>5} 字  {tok(head):>4} token")
    for key in ("intent", "emotion", "danger", "suggestion", "option"):
        text = app_config.field_prompt(cfg, key)
        name = app_config.field_name(cfg, key)
        print(f"  {name:<14} {len(text):>5} 字  {tok(text):>4} token")
    print(f"  TAIL            {len(tail):>5} 字  {tok(tail):>4} token")
    print(f"  风格提示 style   {len(style):>5} 字  {tok(style):>4} token")
    print(f"  合计 full       {len(full) + len(style):>5} 字  {tok(full) + tok(style):>4} token")
    print(f"  合计 slim(本地)  {len(slim) + len(style):>5} 字  {tok(slim) + tok(style):>4} token")
    print(f"  合计 compact(API){len(compact) + len(style):>5} 字  {tok(compact) + tok(style):>4} token")

    print("\n=== 各场景：上下文（真实对话）token 占比 ===")
    for scn in scenarios:
        pool, _v = _pool_and_visible(scn)
        target = _target(scn, pool)
        for variant in ("v0_current", "v1_order", "v2_order_after1"):
            context = assemble(scn, variant, cfg.analyzer.context_messages,
                               cfg.analyzer.context_token_budget)
            prompt = analyzer.build_messages(context, target)
            user = prompt[1]["content"]
            system = prompt[0]["content"]
            st, ut = tok(system), tok(user)
            pct = 100.0 * ut / max(1, st + ut)
            print(f"  {scn['name'][:22]:<22} {variant:<17} system {st:>4} + 上下文 {ut:>4} "
                  f"= {st + ut:>4} token（上下文占 {pct:>4.1f}%，预算 {cfg.analyzer.context_token_budget}）")
    analyzer.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
