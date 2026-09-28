#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/bench_models.py - 三方模型对比（当前 3B / Qwen3-4B / Qwen3-8B）

同一批 10 条真实聊天样本，跑"两段式"（第一段：意图/情绪/危险度/建议；第二段：三条回复），
统计加载时间、两段耗时、danger 分布，并打印意图与建议供人工判断质量。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

WORKSPACE = Path(r"D:\QQChatAssistant")
sys.path.insert(0, str(WORKSPACE))

from config import load_config          # noqa: E402
from core.analyzer import Analyzer      # noqa: E402
from core.message_reader import Message, make_msg_key   # noqa: E402

MODELS = [
    ("Qwen2.5-3B（当前）", WORKSPACE / "models" / "qwen2.5-3b-instruct-q4_k_m.gguf", ""),
    ("Qwen2.5-7B", WORKSPACE / "models" / "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf", ""),
    ("Qwen3-4B", WORKSPACE / "models" / "Qwen3-4B-Q4_K_M.gguf", "/no_think"),
    ("Qwen3-8B", WORKSPACE / "models" / "Qwen3-8B-Q4_K_M.gguf", "/no_think"),
]


def load_samples(limit: int = 10):
    out = []
    for line in open(WORKSPACE / "spikes" / "fixtures" / "llm_inputs.txt", encoding="utf-8-sig"):
        line = line.strip()
        if line and "|" in line:
            t, lab = line.split("|", 1)
            out.append((t.strip(), lab.strip()))
    return out[:limit]


def main() -> int:
    samples = load_samples()
    cfg = load_config()
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
    for name, path, suffix in MODELS:
        if only and only not in name:
            continue
        if not path.exists():
            print(f"跳过（不存在）：{name}")
            continue
        print("=" * 78)
        print(f"### {name}  ({path.stat().st_size / 1024**3:.2f} GB)")
        c = load_config()
        if suffix:
            c.system_prompt = c.system_prompt + "\n" + suffix      # Qwen3: 关掉思考模式
            c.analysis_style_prompt = (c.analysis_style_prompt or "") + " " + suffix
        a = Analyzer(c)
        t0 = time.perf_counter()
        ok = a.load(path)
        load_s = time.perf_counter() - t0
        if not ok:
            print("  加载失败:", a.last_error)
            continue
        print(f"  加载 {load_s:.2f}s | {a.gpu_evidence.get('offloaded')} | "
              f"{' | '.join(a.gpu_evidence.get('buffers', [])[:3])}")
        quick_ms, reply_ms, dangers = [], [], []
        for i, (text, label) in enumerate(samples, 1):
            m = Message(msg_key=make_msg_key("bench", text), text=text, side="left",
                        bbox=(0, i * 30, 200, i * 30 + 24))
            t0 = time.perf_counter()
            q = a.analyze_quick(m, [m], use_cache=False)
            t1 = (time.perf_counter() - t0) * 1000
            r = a.analyze_replies(m, [m], q)
            t2 = (time.perf_counter() - t0) * 1000 - t1
            quick_ms.append(t1)
            reply_ms.append(t2)
            dangers.append(q.danger_level if q else None)
            print(f"  {i:2d}. {text[:22]:<24} 标签={label:<4} "
                  f"意图={q.intent:<16} 情绪={q.emotion:<5} danger={q.danger_level} "
                  f"[{t1:.0f}+{t2:.0f}ms]")
            print(f"      建议：{q.suggestion}")
            print(f"      选项：" + " / ".join(f"[{o.style}]{o.text}" for o in r.replies))
        n = len(quick_ms)
        print(f"  --- 汇总：第一段均 {sum(quick_ms)/n:.0f}ms，第二段均 {sum(reply_ms)/n:.0f}ms，"
              f"danger 分布 {({d: dangers.count(d) for d in sorted(set(dangers))})}")
        a.close()
        del a
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
