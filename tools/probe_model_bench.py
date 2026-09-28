#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/probe_model_bench.py - 1.5B vs 3B 对比：同一批真实样本 + 同一个新 schema（上下文 + 三条选项）。

产出:
  tmp/model_bench.json          机读结果（每条样本的原始输出、耗时、校验结果）
  tmp/model_quality_review.md   给人看的对照清单（两个模型逐条并排）
用法:
  <py3.11> tmp/probe_model_bench.py --models <1.5B.gguf> <3B.gguf>
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

WORKSPACE = Path(r"D:\QQChatAssistant")
sys.path.insert(0, str(WORKSPACE / "spikes"))
import spike_wgc as wgc  # noqa: E402
import spike_llm as llm_probe  # noqa: E402

sys.path.insert(0, str(WORKSPACE / "tmp"))
from probe_llm_v2 import GRAMMAR, SYSTEM_PROMPT  # noqa: E402

OUT_JSON = WORKSPACE / "tmp" / "model_bench.json"
OUT_MD = WORKSPACE / "tmp" / "model_quality_review.md"


def percentile(values, p):
    data = sorted(float(v) for v in values)
    if not data:
        return None
    if len(data) == 1:
        return data[0]
    rank = (p / 100.0) * (len(data) - 1)
    lo, hi = int(rank // 1), int(-(-rank // 1))
    if lo == hi:
        return data[lo]
    return data[lo] + (data[hi] - data[lo]) * (rank - lo)


def make_user_content(text: str, context: str = "") -> str:
    if context:
        return f"对话记录:\n{context}"
    return f"对话记录:\n对方: {text}\n   <<< 需要分析的就是这条"


def validate(payload: dict) -> dict:
    opts = payload.get("reply_options") or []
    styles = [str(o.get("style", "")).strip() for o in opts]
    texts = [str(o.get("text", "")).strip() for o in opts]
    return {
        "options": len(opts),
        "styles_distinct": len(set(styles)) == len(styles) if styles else False,
        "texts_distinct": len(set(texts)) == len(texts) if texts else False,
        "nonempty": all(texts) if texts else False,
        "text_lengths": [len(t) for t in texts],
        "styles": styles,
    }


def run_one(llm, grammar, text: str, context: str = "") -> dict:
    t0 = time.perf_counter()
    first = None
    pieces: list[str] = []
    for chunk in llm.create_chat_completion(
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": make_user_content(text, context)}],
            grammar=grammar, max_tokens=320, temperature=0.2, seed=1234, stream=True):
        if first is None:
            first = time.perf_counter() - t0
        pieces.append((chunk["choices"][0].get("delta", {}) or {}).get("content") or "")
    total = time.perf_counter() - t0
    raw = "".join(pieces)
    record = {"input": text, "raw": raw,
              "first_token_ms": round((first or 0) * 1000, 1),
              "total_ms": round(total * 1000, 1)}
    try:
        payload = json.loads(raw)
        record["json_ok"] = True
        record.update(validate(payload))
        record["fields"] = {k: payload.get(k) for k in
                            ("intent", "emotion", "danger_level", "suggestion", "confidence")}
    except Exception as exc:
        record["json_ok"] = False
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


def bench_model(model_path: Path, samples: list[dict], context_sample: dict | None,
                max_tokens: int = 320) -> dict:
    llm_probe.prepare_cuda_dll_dirs()
    import llama_cpp
    from llama_cpp import Llama, LlamaGrammar

    llm_probe.register_llama_log_capture(llama_cpp)
    del llm_probe._LLAMA_LOG_LINES[:]
    t_load = time.perf_counter()
    llm = Llama(model_path=str(model_path), n_ctx=2048, n_gpu_layers=-1,
                seed=1234, temperature=0.2, verbose=True)
    load_s = time.perf_counter() - t_load
    vram_lines = [l for l in llm_probe._LLAMA_LOG_LINES
                  if "model buffer size" in l or "KV buffer size" in l
                  or "compute buffer size" in l or "offloaded" in l]

    grammar = LlamaGrammar.from_string(GRAMMAR, verbose=False)
    del llm_probe._LLAMA_LOG_LINES[:]
    for _ in llm.create_chat_completion(
            messages=[{"role": "system", "content": "只输出 JSON。"},
                      {"role": "user", "content": "测试"}],
            grammar=grammar, max_tokens=8, temperature=0.0, stream=True):
        pass
    grammar_errors = [l for l in llm_probe._LLAMA_LOG_LINES if "error parsing grammar" in l]

    records = [run_one(llm, grammar, s["text"]) for s in samples]
    ctx_record = run_one(llm, grammar, context_sample["target"],
                         context_sample["context"]) if context_sample else None

    totals = [r["total_ms"] for r in records]
    firsts = [r["first_token_ms"] for r in records]
    summary = {
        "model": model_path.name,
        "size_gb": round(model_path.stat().st_size / 1024 ** 3, 3),
        "load_s": round(load_s, 3),
        "grammar_ok": not grammar_errors,
        "vram_evidence": vram_lines[-4:],
        "samples": len(records),
        "json_ok_rate": round(sum(1 for r in records if r["json_ok"]) / len(records), 3),
        "options3_rate": round(sum(1 for r in records if r.get("options") == 3) / len(records), 3),
        "texts_distinct_rate": round(
            sum(1 for r in records if r.get("texts_distinct")) / len(records), 3),
        "total_ms_p50": round(percentile(totals, 50), 1),
        "total_ms_p95": round(percentile(totals, 95), 1),
        "total_ms_mean": round(statistics.fmean(totals), 1),
        "first_token_ms_mean": round(statistics.fmean(firsts), 1),
    }
    del llm
    return {"summary": summary, "records": records, "context_sample": ctx_record}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    samples, malformed = llm_probe.load_llm_inputs(llm_probe.LLM_INPUTS)
    if args.limit:
        samples = samples[: args.limit]
    print(f"真实样本 {len(samples)} 条（异常 {len(malformed)}）")

    # 真实上下文样例：复用之前从 UIA 读到的会话
    context_sample = None
    cached = WORKSPACE / "tmp" / "probe_llm_v2.json"
    if cached.exists():
        data = json.loads(cached.read_text(encoding="utf-8"))
        ctx = data.get("context", "")
        msgs = data.get("messages", [])
        lefts = [m for m in msgs if m.get("side") == "left"]
        if ctx and lefts:
            context_sample = {"context": ctx, "target": lefts[-1]["text"]}

    results = {}
    for model in args.models:
        path = Path(model)
        if not path.exists():
            print(f"跳过（不存在）: {path}")
            continue
        print(f"\n===== 跑 {path.name}（{path.stat().st_size / 1024**3:.2f} GB）=====")
        t0 = time.time()
        results[path.name] = bench_model(path, samples, context_sample)
        s = results[path.name]["summary"]
        print(f"  加载 {s['load_s']}s；JSON {s['json_ok_rate']*100:.0f}%；"
              f"3 选项 {s['options3_rate']*100:.0f}%；文本互不相同 {s['texts_distinct_rate']*100:.0f}%")
        print(f"  单条总耗时 p50={s['total_ms_p50']}ms p95={s['total_ms_p95']}ms；"
              f"首 token 均值 {s['first_token_ms_mean']}ms；总用时 {time.time()-t0:.0f}s")

    OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    # 人看的对照清单
    lines = ["# 1.5B vs 3B 对比（同一批真实样本 / 同一 schema：上下文 + 三条选项）", ""]
    for name, data in results.items():
        s = data["summary"]
        lines += [f"## {name}（{s['size_gb']} GB）", "",
                  f"- 加载 {s['load_s']}s；JSON 合法 {s['json_ok_rate']*100:.0f}%；"
                  f"三条选项齐全 {s['options3_rate']*100:.0f}%；文本互不相同 {s['texts_distinct_rate']*100:.0f}%",
                  f"- 单条耗时 p50 {s['total_ms_p50']}ms / p95 {s['total_ms_p95']}ms；"
                  f"首 token 均值 {s['first_token_ms_mean']}ms",
                  f"- 显存证据: {' | '.join(s['vram_evidence'])}", ""]
        for i, r in enumerate(data["records"], 1):
            f = r.get("fields", {})
            lines.append(f"{i}. **{r['input']}**")
            if r.get("json_ok"):
                lines.append(f"   - intent={f.get('intent')} | emotion={f.get('emotion')} | "
                             f"danger={f.get('danger_level')} | conf={f.get('confidence')} | "
                             f"{r['total_ms']}ms")
                lines.append(f"   - 建议：{f.get('suggestion')}")
                for st, tx in zip(r.get("styles", []), [o for o in
                                                        [x for x in []]] or []):
                    pass
                opts = json.loads(r["raw"]).get("reply_options", [])
                for o in opts:
                    lines.append(f"   - [{o.get('style')}] {o.get('text')}")
            else:
                lines.append(f"   - ❌ JSON 不合法：{r.get('error')}（{r['total_ms']}ms）")
            lines.append("")
        if data.get("context_sample"):
            c = data["context_sample"]
            lines += ["### 带上下文（真实会话）", "",
                      f"- 上下文：\n```\n{c['input']}\n```", ""]
            if c.get("json_ok"):
                payload = json.loads(c["raw"])
                lines.append(f"- intent={payload.get('intent')} | emotion={payload.get('emotion')} | "
                             f"danger={payload.get('danger_level')} | {c['total_ms']}ms")
                lines.append(f"- 建议：{payload.get('suggestion')}")
                for o in payload.get("reply_options", []):
                    lines.append(f"- [{o.get('style')}] {o.get('text')}")
            else:
                lines.append(f"- ❌ JSON 不合法：{c.get('error')}")
            lines.append("")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print("\n结果:", OUT_JSON)
    print("人看清单:", OUT_MD)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
