#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/probe_llm_v2.py - 可行性探针：UIA 读到的上下文 + 三条不同风格回复选项（新 GBNF）。

验证三件事：
  1. 用 UIA 真实读到的可见消息拼上下文（对方/我 + 最新对方消息）是否可行、够不够 token；
  2. 新 GBNF（intent/emotion/danger_level/suggestion/reply_options[3]/confidence）能否被 llama.cpp 接受；
  3. 输出质量：三条回复的风格是否真的不同、是否可直接发送。
结果写 tmp/probe_llm_v2.json（UTF-8）。
"""
from __future__ import annotations

import ctypes
import json
import sys
import time
from pathlib import Path

WORKSPACE = Path(r"D:\QQChatAssistant")
sys.path.insert(0, str(WORKSPACE / "spikes"))
import spike_wgc as wgc  # noqa: E402
import spike_uia as uia_probe  # noqa: E402  （复用窗口定位/读取/激活逻辑）
import spike_llm as llm_probe  # noqa: E402  （复用 CUDA DLL 注入）

OUT = WORKSPACE / "tmp" / "probe_llm_v2.json"
user32 = ctypes.WinDLL("user32", use_last_error=True)

GRAMMAR = r'''
root ::= "{" ws k1 k2 k3 k4 k5 k6 ws "}"
k1 ::= "\"intent\"" ws ":" ws str14
k2 ::= "," ws "\"emotion\"" ws ":" ws str10
k3 ::= "," ws "\"danger_level\"" ws ":" ws danger
k4 ::= "," ws "\"suggestion\"" ws ":" ws str80
k5 ::= "," ws "\"reply_options\"" ws ":" ws "[" ws opt ws "," ws opt ws "," ws opt ws "]"
k6 ::= "," ws "\"confidence\"" ws ":" ws conf
opt ::= "{" ws "\"style\"" ws ":" ws str6 ws "," ws "\"text\"" ws ":" ws str80 ws "}"
danger ::= "0" | [1-9] | "10"
conf ::= ("0" | "1") ("." [0-9]{1,4})?
str6 ::= "\"" jchar{1,6} "\""
str10 ::= "\"" jchar{1,10} "\""
str14 ::= "\"" jchar{1,14} "\""
str80 ::= "\"" jchar{1,80} "\""
jchar ::= [^"\\\x00-\x1F]
ws ::= [ \t\n]*
'''

SYSTEM_PROMPT = (
    "你是QQ聊天分析助手。输入是一段对话记录（“我”是用户自己，“对方”是聊天对象），"
    "最后一行标注了需要分析的那条对方消息。请结合上下文理解它，然后输出 JSON：\n"
    "intent：用自己的话概括对方这条消息想干什么，最多 14 个字；\n"
    "emotion：用不超过 10 个字的词描述对方情绪；\n"
    "danger_level：0-10 整数，按锚点打分——0-2 纯日常（寒暄/约事/答话）；"
    "3-4 有情绪但不针对你（普通请求/吐槽/轻微不满）；5-6 明确的不满、催促、追问；"
    "7-8 指向你的追责、翻旧账、质疑；9-10 明确冲突（最后通牒/攻击）；拿不准宁可给低；\n"
    "suggestion：给用户看的分析建议，不超过 80 字；\n"
    "reply_options：给出恰好三条可直接发送的回复，风格要明显不同"
    "（例如 稳妥/简短/轻松 之类，style 不超过 6 个字，text 不超过 80 字）；\n"
    "confidence：0-1 的小数。\n"
    "所有文字用纯文本：不要前缀、不要 Markdown、不要换行。只输出 JSON。"
)


def build_context(messages: list[dict], target_index: int, max_msgs: int = 10) -> str:
    window = messages[max(0, target_index - max_msgs + 1): target_index + 1]
    lines = []
    for i, msg in enumerate(window):
        who = "对方" if msg["side"] == "left" else "我"
        mark = "   <<< 需要分析的就是这条" if i == len(window) - 1 else ""
        lines.append(f"{who}: {msg['text']}{mark}")
    return "\n".join(lines)


def main() -> int:
    wgc.set_dpi_awareness()
    win = uia_probe.pick_chat_window()
    if not win:
        print("没有可见的 QQ 聊天窗口")
        return 1
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.uia_element_info import UIAElementInfo

    wrapper = UIAWrapper(UIAElementInfo(win["hwnd"]))
    listener = uia_probe.register_uia_listener(win["hwnd"])
    for _ in range(6):
        result = uia_probe.read_messages(wrapper)
        if result.get("ok") and result["messages"]:
            break
        time.sleep(1.0)
    messages = result.get("messages", [])
    print(f"UIA 读到 {len(messages)} 条消息（backend 就绪={result.get('ok')}）")
    for m in messages:
        print(f"  [{m['side']}] {m['text'][:36]}")

    lefts = [i for i, m in enumerate(messages) if m["side"] == "left"]
    if not lefts:
        print("可见消息里没有对方(左)消息，无法测上下文分析")
        return 1
    target_index = lefts[-1]
    context = build_context(messages, target_index)
    print("--- 上下文 ---")
    print(context)

    # 复用 spike_llm 的 CUDA DLL 注入 + llama.cpp 日志回调（用于语法自检）
    llm_probe.prepare_cuda_dll_dirs()
    import llama_cpp
    from llama_cpp import Llama, LlamaGrammar

    llm_probe.register_llama_log_capture(llama_cpp)
    model_path = WORKSPACE / "models" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
    llm = Llama(model_path=str(model_path), n_ctx=2048, n_gpu_layers=-1,
                seed=1234, temperature=0.2, verbose=False)
    grammar = LlamaGrammar.from_string(GRAMMAR, verbose=False)

    # 语法自检（llama.cpp 惰性解析）
    del llm_probe._LLAMA_LOG_LINES[:]
    for _ in llm.create_chat_completion(
            messages=[{"role": "system", "content": "只输出 JSON。"},
                      {"role": "user", "content": "测试"}],
            grammar=grammar, max_tokens=8, temperature=0.0, stream=True):
        pass
    errors = [line for line in llm_probe._LLAMA_LOG_LINES if "error parsing grammar" in line]
    print("语法自检:", "OK" if not errors else errors[0])
    if errors:
        return 1

    messages_payload = [{"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": f"对话记录:\n{context}"}]
    prompt_tokens = len(llm.tokenize(
        (SYSTEM_PROMPT + context).encode("utf-8"), add_bos=False))
    t0 = time.perf_counter()
    first = None
    pieces = []
    for chunk in llm.create_chat_completion(messages=messages_payload, grammar=grammar,
                                            max_tokens=320, temperature=0.2,
                                            seed=1234, stream=True):
        if first is None:
            first = time.perf_counter() - t0
        pieces.append((chunk["choices"][0].get("delta", {}) or {}).get("content") or "")
    total = time.perf_counter() - t0
    raw = "".join(pieces)
    print(f"prompt≈{prompt_tokens} token；首token {first*1000:.0f}ms；总 {total*1000:.0f}ms")
    print("--- 模型输出 ---")
    print(raw)

    try:
        payload = json.loads(raw)
        styles = [opt.get("style") for opt in payload.get("reply_options", [])]
        texts = [opt.get("text") for opt in payload.get("reply_options", [])]
        summary = {"json_ok": True, "options": len(texts),
                   "styles_unique": len(set(styles)) == len(styles),
                   "styles": styles, "text_lengths": [len(t or "") for t in texts]}
    except Exception as exc:
        summary = {"json_ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print("小结:", json.dumps(summary, ensure_ascii=False))

    OUT.write_text(json.dumps({"window": win, "messages": messages, "context": context,
                               "prompt_tokens": prompt_tokens,
                               "first_token_ms": round((first or 0) * 1000, 1),
                               "total_ms": round(total * 1000, 1),
                               "raw_output": raw, "summary": summary},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写:", OUT)
    uia_probe.remove_uia_listener(listener)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
