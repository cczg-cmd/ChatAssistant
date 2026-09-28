#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/probe_intent.py - 临时探针：GBNF 字段顺序 / 是否顺序无关，对意图命中率的影响。

背景：spike_llm 的 enum 语法把字段顺序写死为 danger/emotion/intent/...，
实测前 5 条里模型把"意图类"的词（求助/询问）填进了 emotion，intent 则偏向"求助"。
本探针在同一模型实例上对比三种语法形态，看命中率差异（不参与最终判定，只做设计决策）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

WORKSPACE = Path(r"D:\QQChatAssistant")
sys.path.insert(0, str(WORKSPACE / "spikes"))
import spike_llm as S  # noqa: E402

S.prepare_cuda_dll_dirs()
import llama_cpp  # noqa: E402
from llama_cpp import Llama, LlamaGrammar  # noqa: E402

S.register_llama_log_capture(llama_cpp)
MODEL = str(WORKSPACE / "models" / "qwen2.5-1.5b-instruct-q4_k_m.gguf")

TAIL = r'''
danger ::= "0" | [1-9] | "10"
conf ::= ("0" | "1") ("." [0-9]{1,4})?
str10 ::= "\"" jchar{1,10} "\""
str20 ::= "\"" jchar{1,20} "\""
str80 ::= "\"" jchar{1,80} "\""
str120 ::= "\"" jchar{1,120} "\""
jchar ::= [^"\\\x00-\x1F]
ws ::= [ \t\n]*
intentenum ::= "\"" ("求助" | "吐槽" | "闲聊" | "争议" | "信息分享" | "其他") "\""
'''

# V1 = 现在的实现：danger, emotion, intent, ...
V1 = S.build_grammar("enum")

# V2 = intent 紧跟 danger_level
V2 = r'''
root ::= "{" ws dk it em sg rp cf ws "}"
dk ::= "\"danger_level\"" ws ":" ws danger
it ::= "," ws "\"intent\"" ws ":" ws intentenum
em ::= "," ws "\"emotion\"" ws ":" ws str10
sg ::= "," ws "\"suggestion\"" ws ":" ws str80
rp ::= "," ws "\"suggested_reply\"" ws ":" ws str120
cf ::= "," ws "\"confidence\"" ws ":" ws conf
''' + TAIL

# V3 = 顺序无关：每个键自带取值规则，任意顺序、任意子集（Python 层校验齐全性）
V3 = r'''
root ::= "{" ws members? ws "}"
members ::= member (ws "," ws member)*
member ::= kdanger | kemotion | kintent | ksuggestion | kreply | kconf
kdanger ::= "\"danger_level\"" ws ":" ws danger
kemotion ::= "\"emotion\"" ws ":" ws str10
kintent ::= "\"intent\"" ws ":" ws intentenum
ksuggestion ::= "\"suggestion\"" ws ":" ws str80
kreply ::= "\"suggested_reply\"" ws ":" ws str120
kconf ::= "\"confidence\"" ws ":" ws conf
''' + TAIL

VARIANTS = {"V1_order_current": V1, "V2_order_intent_first": V2, "V3_order_agnostic": V3}

# V4 = intent 放最前（先做最重要的分类，再补 danger/emotion）
V4 = r'''
root ::= "{" ws it ws dk ws em ws sg ws rp ws cf ws "}"
it ::= "\"intent\"" ws ":" ws intentenum
dk ::= "," ws "\"danger_level\"" ws ":" ws danger
em ::= "," ws "\"emotion\"" ws ":" ws str10
sg ::= "," ws "\"suggestion\"" ws ":" ws str80
rp ::= "," ws "\"suggested_reply\"" ws ":" ws str120
cf ::= "," ws "\"confidence\"" ws ":" ws conf
''' + TAIL

SHORT_PROMPT = (
    "你是QQ聊天分析助手。读一条对方发来的消息，输出 JSON：\n"
    "intent：从「求助/吐槽/闲聊/争议/信息分享/其他」里选一个（求助=要你帮忙或提问，"
    "吐槽=抱怨发泄，闲聊=寒暄约事，争议=质疑追责，信息分享=告知新信息，其他=确认致谢等）；\n"
    "emotion：对方情绪，<=10字；danger_level：0-10 整数，越大越容易起冲突；"
    "suggestion：<=80字建议；suggested_reply：<=120字可直接回复。\n"
    "只输出 JSON。"
)

V5 = V4
VARIANTS = {"V2_order_intent_first": V2, "V4_intent_very_first": V4, "V5_short_prompt": V5}


def run_once(llm: Llama, grammar, sample: dict, prompt: str | None = None) -> dict:
    messages = S.build_messages(sample["text"], "enum")
    if prompt is not None:
        messages[0] = {"role": "system", "content": prompt}
    t0 = time.perf_counter()
    first = None
    pieces = []
    stream = llm.create_chat_completion(messages=messages, grammar=grammar,
                                        max_tokens=320, temperature=0.2,
                                        seed=1234, stream=True)
    for chunk in stream:
        if first is None:
            first = time.perf_counter() - t0
        pieces.append((chunk["choices"][0].get("delta", {}) or {}).get("content") or "")
    total = time.perf_counter() - t0
    raw = "".join(pieces)
    parsed = S.parse_model_json(raw, "enum")
    fields = parsed["fields"] or {}
    hit, kind = S.intent_matches(str(fields.get("intent", "")), sample["label"])
    return {"raw": raw, "ok": parsed["ok"], "intent": fields.get("intent"),
            "emotion": fields.get("emotion"), "hit": hit, "kind": kind,
            "ms": round(total * 1000), "first_ms": round((first or total) * 1000)}


def main() -> int:
    samples, bad = S.load_llm_inputs(S.LLM_INPUTS)
    print("samples", len(samples), "malformed", bad)
    llm = Llama(model_path=MODEL, n_ctx=2048, n_gpu_layers=-1, seed=1234,
                temperature=0.2, verbose=False)
    summary = {}
    dump = {}
    for name, text in VARIANTS.items():
        grammar = LlamaGrammar.from_string(text, verbose=False)
        del S._LLAMA_LOG_LINES[:]
        prompt = SHORT_PROMPT if name == "V5_short_prompt" else None
        records = [run_once(llm, grammar, s, prompt) for s in samples]
        errs = S.grammar_parse_errors(S._LLAMA_LOG_LINES)
        hits = sum(1 for r in records if r["hit"])
        matches = sum(1 for r in records if r["hit"] and r["kind"] == "exact")
        valid = sum(1 for r in records if r["ok"])
        summary[name] = {"hits": hits, "exact": matches, "valid": valid, "samples": len(records),
                         "grammar_errors": errs}
        dump[name] = [{**r, "label": s["label"], "text": s["text"]}
                      for s, r in zip(samples, records)]
        print(f"\n=== {name}: json={valid}/{len(records)} hit={hits}/{len(records)}"
              f" grammar_errors={len(errs)}")
        for i, (s, r) in enumerate(zip(samples, records), 1):
            flag = "HIT " if r["hit"] else "MISS"
            print(f"  [{flag}] {i:2d} label={s['label']:<4} intent={str(r['intent']):<6}"
                  f" emotion={str(r['emotion']):<6} {r['ms']}ms  {s['text'][:24]}")
    print("\nSUMMARY", json.dumps(summary, ensure_ascii=False))
    out = WORKSPACE / "tmp" / "probe_intent_result.json"
    out.write_text(json.dumps({"summary": summary, "records": dump},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
