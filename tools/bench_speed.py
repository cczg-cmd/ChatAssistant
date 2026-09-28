#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""速度实测：相同样本、相同提示词，只换推理设置，量出第一段/第二段耗时与 tok/s。

对照项：
  baseline      当前口径（n_ctx=2048，其余默认）
  flash_attn    开启 flash attention（llama.cpp 更快，显存更省）
  flash+batch   再加 n_batch/n_ubatch=1024（预填充更快）
  kv_q8         KV cache 量化成 q8_0（省显存；看是否会掉速）
  3B/3B-flash   同设置换 3B 模型（对比"速度换智力"）
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils import win32_api as w32  # noqa: E402

w32.set_dpi_awareness()

import config as app_config  # noqa: E402
from core import analyzer as analyzer_mod  # noqa: E402
from core.analyzer import Analyzer  # noqa: E402
from core.message_reader import Message  # noqa: E402

# 先注入 CUDA 运行库目录（与 Analyzer.load 同款），再 import llama_cpp，
# 否则 ctypes 找不到 llama.dll 的依赖（cublas/cudart）。
import os as _os  # noqa: E402

_cfg_for_dll = app_config.load_config()
_cuda_dirs = analyzer_mod._cuda_bin_dirs(_cfg_for_dll)
for _directory in _cuda_dirs:
    try:
        _os.add_dll_directory(_directory)
    except Exception:
        pass
_os.environ["PATH"] = ";".join(_cuda_dirs) + ";" + _os.environ.get("PATH", "")

from llama_cpp import Llama, LlamaGrammar  # noqa: E402

MODEL_7B = ROOT / "models" / "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf"
MODEL_3B = ROOT / "models" / "qwen2.5-3b-instruct-q4_k_m.gguf"
MODEL_QWEN3_4B = ROOT / "models" / "qwen3-4b-instruct-2507-q4_k_m.gguf"

BRIEF_STYLE = (
    "输出必须极简，这是硬要求：suggestion 不超过 18 个字；"
    "三条 reply 每条不超过 20 个字、不要解释、不要客套；"
    "intent 不超过 10 个字。宁可短也别啰嗦。"
)

SAMPLES = [
    ("我现在打开游戏就会掉网",
     ["对方: 你电脑有去清过灰嘛", "我: 没有啊", "对方: 我电脑之前也这样，清了灰就好了"]),
    ("问豆包说去清灰试试",
     ["对方: 我现在打开游戏就会掉网", "我: 那怎么办"]),
]

CONFIGS = [
    # 标注：都先做一次"预热"生成（不计时），排除冷启动的 kernel/预填充开销
    ("7B 现口径", MODEL_7B, {}, None, None),
    ("Qwen3-4B-2507", MODEL_QWEN3_4B, {}, None, None),
    ("3B 现口径", MODEL_3B, {}, None, None),
]


def make_ctx(target_text: str, background: list) -> tuple:
    target = Message(msg_key="bench-target", text=target_text, side="left", bbox=(0, 0, 300, 40))
    ctx = [target]
    for index, line in enumerate(background):
        side = "left" if line.startswith("对方") else "right"
        ctx.append(Message(msg_key=f"bg{index}", text=line.split(": ", 1)[-1], side=side,
                           bbox=(0, 0, 0, 0)))
    return target, ctx


def bench_one(analyzer, label: str, extra: dict, model: Path, limits: dict = None,
              style: str = None) -> dict:
    cfg = analyzer.cfg
    a = cfg.analyzer
    kwargs = dict(model_path=str(model), n_ctx=a.n_ctx, n_gpu_layers=a.n_gpu_layers,
                  seed=a.seed, temperature=a.temperature, verbose=False)
    kwargs.update(extra)
    t0 = time.perf_counter()
    try:
        llm = Llama(**kwargs)
    except Exception as exc:
        return {"label": label, "error": f"{type(exc).__name__}: {exc}"}
    load_s = round(time.perf_counter() - t0, 2)
    analyzer.llm = llm
    analyzer.grammar = LlamaGrammar.from_string(analyzer_mod.build_grammar(cfg), verbose=False)
    analyzer.grammar_quick = LlamaGrammar.from_string(analyzer_mod.build_quick_grammar(cfg),
                                                      verbose=False)
    analyzer.grammar_reply = LlamaGrammar.from_string(analyzer_mod.build_reply_grammar(cfg),
                                                      verbose=False)
    if limits:
        cfg.field_limits.update(limits)
        # 语法里写死了长度上限，改完必须重建语法
        analyzer.grammar = LlamaGrammar.from_string(analyzer_mod.build_grammar(cfg), verbose=False)
        analyzer.grammar_quick = LlamaGrammar.from_string(
            analyzer_mod.build_quick_grammar(cfg), verbose=False)
        analyzer.grammar_reply = LlamaGrammar.from_string(
            analyzer_mod.build_reply_grammar(cfg), verbose=False)
    if style == "brief":
        cfg.analysis_style_prompt = BRIEF_STYLE
    rows = []
    # 预热：每个配置先跑一次，避免把冷启动的 kernel/全量预填充算进成绩
    warm_target, warm_ctx = make_ctx("这是热身消息，用来把 CUDA kernel 预热", [])
    analyzer._cache.clear()
    analyzer.analyze_quick(warm_target, warm_ctx, use_cache=False)
    for target_text, background in SAMPLES:
        target, ctx = make_ctx(target_text, background)
        analyzer._cache.clear()
        quick = analyzer.analyze_quick(target, ctx, use_cache=False)
        if quick is None:
            rows.append({"target": target_text, "error": "第一段失败"})
            continue
        replies = analyzer.analyze_replies(target, ctx, quick=quick)
        rows.append({
            "target": target_text,
            "intent": quick.intent,
            "quick_ms": quick.total_ms,
            "quick_prompt_tokens": analyzer.last_prompt_tokens,
            "quick_out_tokens": analyzer.last_completion_tokens,
            "quick_tok_s": (round(analyzer.last_completion_tokens / (quick.total_ms / 1000), 1)
                            if analyzer.last_completion_tokens else None),
            "reply_ms": None if replies is None else replies.total_ms,
            "reply_out_tokens": analyzer.last_completion_tokens,
            "reply_tok_s": (round(analyzer.last_completion_tokens / (replies.total_ms / 1000), 1)
                            if (replies and analyzer.last_completion_tokens) else None),
        })
    try:
        del llm
    except Exception:
        pass
    return {"label": label, "model": model.name, "load_s": load_s, "rows": rows}


def main() -> int:
    cfg = app_config.load_config()
    analyzer = Analyzer(cfg)
    results = []
    for label, model, extra, limits, style in CONFIGS:
        print(f"\n===== {label}（{model.name}）=====", flush=True)
        cfg.field_limits.update(app_config.FIELD_LIMITS)          # 复位长度上限
        cfg.analysis_style_prompt = app_config.DEFAULT_STYLE_PROMPT   # 复位风格
        out = bench_one(analyzer, label, extra, model, limits, style)
        if "error" in out:
            print(f"  ! 失败：{out['error']}")
            results.append(out)
            continue
        print(f"  加载 {out['load_s']}s")
        for row in out["rows"]:
            if "error" in row:
                print(f"  ! {row['target']}：{row['error']}")
                continue
            print(f"  「{row['target']}」intent={row['intent']}")
            print(f"     第一段 {row['quick_ms']:.0f}ms（prompt {row['quick_prompt_tokens']} tok，"
                  f"输出 {row['quick_out_tokens']} tok，{row['quick_tok_s']} tok/s）")
            print(f"     第二段 {row['reply_ms']:.0f}ms（输出 {row['reply_out_tokens']} tok，"
                  f"{row['reply_tok_s']} tok/s）")
        totals = [r["quick_ms"] + (r["reply_ms"] or 0) for r in out["rows"] if "error" not in r]
        out["avg_total_ms"] = round(sum(totals) / len(totals), 1) if totals else None
        print(f"  两段合计平均：{out['avg_total_ms']:.0f}ms")
        results.append(out)
    Path(ROOT / "tmp" / "bench_speed.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n===== 汇总（两段合计平均）=====")
    for out in results:
        if "error" in out:
            print(f"{out['label']:<14} 失败：{out['error'][:70]}")
        else:
            print(f"{out['label']:<14} {out['avg_total_ms']:>7.0f}ms  加载 {out['load_s']}s"
                  f"  {out['model']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
