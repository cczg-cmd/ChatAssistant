#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tmp/probe_llm.py - 临时探针：确认 llama_cpp 0.3.4 的 API / GPU offload / 语法约束可用性。

只用于开发期验证，不属于产物清单；跑完即删。
"""
from __future__ import annotations

import inspect
import os
import sys
import time

WORKSPACE = r"D:\QQChatAssistant"
PY311_SP = os.path.join(WORKSPACE, "cache", "py311", "Lib", "site-packages")
NVIDIA_ROOT = os.path.join(PY311_SP, "nvidia")

MODEL = os.path.join(WORKSPACE, "models", "qwen2.5-1.5b-instruct-q4_k_m.gguf")


def dll_dirs() -> list[str]:
    found = []
    if os.path.isdir(NVIDIA_ROOT):
        for name in sorted(os.listdir(NVIDIA_ROOT)):
            bin_dir = os.path.join(NVIDIA_ROOT, name, "bin")
            if os.path.isdir(bin_dir):
                found.append(bin_dir)
    return found


def main() -> int:
    for d in dll_dirs():
        os.add_dll_directory(d)
    os.environ["PATH"] = ";".join(dll_dirs()) + ";" + os.environ["PATH"]

    import llama_cpp
    from llama_cpp import Llama, LlamaGrammar

    print("python", sys.version.split()[0])
    print("llama_cpp", llama_cpp.__version__)
    print("supports_gpu_offload", llama_cpp.llama_supports_gpu_offload())
    print("system_info", llama_cpp.llama_print_system_info().decode("utf-8", "ignore"))
    print("create_chat_completion signature:")
    print("   ", inspect.signature(Llama.create_chat_completion))

    t0 = time.time()
    llm = Llama(model_path=MODEL, n_ctx=2048, n_gpu_layers=-1,
                seed=1234, temperature=0.2, verbose=True)
    print("load_s", round(time.time() - t0, 2))
    print("n_gpu_layers attr", getattr(llm, "n_gpu_layers", None))
    print("n_ctx", llm.n_ctx())
    meta_keys = sorted(llm.metadata.keys())
    print("metadata key count", len(meta_keys))
    print("has tokenizer.chat_template", "tokenizer.chat_template" in llm.metadata)
    print("general.name", llm.metadata.get("general.name"))
    print("qwen.arch", llm.metadata.get("general.architecture"))

    gbnf = r'''
root ::= "{" ws "\"danger_level\"" ws ":" ws danger "," ws "\"emotion\"" ws ":" ws str10 "," ws "\"intent\"" ws ":" ws intent "," ws "\"suggestion\"" ws ":" ws str80 "," ws "\"suggested_reply\"" ws ":" ws str120 "," ws "\"confidence\"" ws ":" ws conf ws "}"
danger ::= "0" | [1-9] | "10"
conf ::= ("0" | "1") ("." [0-9]{1,4})?
intent ::= "\"" ("求助" | "吐槽" | "闲聊" | "争议" | "信息分享" | "其他") "\""
str10 ::= "\"" jchar{1,10} "\""
str80 ::= "\"" jchar{1,80} "\""
str120 ::= "\"" jchar{1,120} "\""
jchar ::= [^"\\\x00-\x1F]
ws ::= [ \t\n]*
'''
    messages = [
        {"role": "system", "content": "你是QQ聊天分析助手，只输出JSON。"},
        {"role": "user", "content": "分析这条聊天内容：在吗？帮我看看这个报错"},
    ]
    t0 = time.time()
    first = None
    chunks_text = []
    usage = None
    grammar = LlamaGrammar.from_string(gbnf, verbose=True)
    print("grammar ok")
    stream = llm.create_chat_completion(messages=messages, grammar=grammar,
                                        max_tokens=320, temperature=0.2,
                                        stream=True)
    for chunk in stream:
        if first is None:
            first = time.time() - t0
        delta = chunk["choices"][0].get("delta", {})
        chunks_text.append(delta.get("content") or "")
        if chunk.get("usage"):
            usage = chunk["usage"]
    total = time.time() - t0
    text = "".join(chunks_text)
    print("first_token_s", round(first, 3) if first else None)
    print("total_s", round(total, 3))
    print("usage", usage)
    print("raw_output", repr(text))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
